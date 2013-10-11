# Core modules
import os
import re
import logging
import subprocess
import sys
import time
import datetime
import socket
import signal

import modules

from util import get_os, get_uuid, md5, Timer, get_hostname, EC2
from config import get_version, get_system_stats

import checks.system.unix as u
import checks.system.win32 as w32
from checks.agent_metrics import CollectorMetrics
from checks.ganglia import Ganglia
from checks.nagios import Nagios
from checks.datadog import Dogstreams, DdForwarder
from checks.check_status import CheckStatus, CollectorStatus, EmitterStatus
from resources.processes import Processes as ResProcesses


log = logging.getLogger(__name__)


FLUSH_LOGGING_PERIOD = 10
FLUSH_LOGGING_INITIAL = 5

class Collector(object):
    """
    The collector is responsible for collecting data from each check and
    passing it along to the emitters, who send it to their final destination.
    """

    def __init__(self, agentConfig, emitters, systemStats):
        self.emit_duration = None
        self.agentConfig = agentConfig
        # system stats is generated by config.get_system_stats
        self.agentConfig['system_stats'] = systemStats
        # agent config is used during checks, system_stats can be accessed through the config
        self.os = get_os()
        self.plugins = None
        self.emitters = emitters            
        self.metadata_interval = int(agentConfig.get('metadata_interval', 10 * 60))
        self.metadata_start = time.time()
        socket.setdefaulttimeout(15)
        self.run_count = 0
        self.continue_running = True
        self.metadata_cache = None
        self.initialized_checks_d = []
        self.init_failed_checks_d = []

        # Unix System Checks
        self._unix_system_checks = {
            'disk': u.Disk(log),
            'io': u.IO(log),
            'load': u.Load(log),
            'memory': u.Memory(log),
            'processes': u.Processes(log),
            'cpu': u.Cpu(log)
        }

        # Win32 System `Checks
        self._win32_system_checks = {
            'disk': w32.Disk(log),
            'io': w32.IO(log),
            'proc': w32.Processes(log),
            'memory': w32.Memory(log),
            'network': w32.Network(log),
            'cpu': w32.Cpu(log)
        }

        # Old-style metric checks
        self._ganglia = Ganglia(log)
        self._dogstream = Dogstreams.init(log, self.agentConfig)
        self._ddforwarder = DdForwarder(log, self.agentConfig)

        # Agent Metrics
        self._agent_metrics = CollectorMetrics(log)

        self._metrics_checks = []

        # Custom metric checks
        for module_spec in [s.strip() for s in self.agentConfig.get('custom_checks', '').split(',')]:
            if len(module_spec) == 0: continue
            try:
                self._metrics_checks.append(modules.load(module_spec, 'Check')(log))
                log.info("Registered custom check %s" % module_spec)
            except Exception, e:
                log.exception('Unable to load custom check module %s' % module_spec)

        # Event Checks
        self._event_checks = [
            Nagios(get_hostname()),
        ]

        # Resource Checks
        self._resources_checks = [
            ResProcesses(log,self.agentConfig)
        ]

    def stop(self):
        """
        Tell the collector to stop at the next logical point.
        """
        # This is called when the process is being killed, so 
        # try to stop the collector as soon as possible.
        # Most importantly, don't try to submit to the emitters
        # because the forwarder is quite possibly already killed
        # in which case we'll get a misleading error in the logs.
        # Best to not even try.
        self.continue_running = False
        for check in self.initialized_checks_d:
            check.stop()
    
    def run(self, checksd=None, start_event=True):
        """
        Collect data from each check and submit their data.
        """
        timer = Timer()
        if self.os != 'windows':
            cpu_clock = time.clock()
        self.run_count += 1
        log.debug("Starting collection run #%s" % self.run_count)

        payload = self._build_payload(start_event=start_event)
        metrics = payload['metrics']
        events = payload['events']
        if checksd:
            self.initialized_checks_d = checksd['initialized_checks'] # is of type {check_name: check}
            self.init_failed_checks_d = checksd['init_failed_checks'] # is of type {check_name: {error, traceback}}
        # Run the system checks. Checks will depend on the OS
        if self.os == 'windows':
            # Win32 system checks
            try:
                metrics.extend(self._win32_system_checks['disk'].check(self.agentConfig))
                metrics.extend(self._win32_system_checks['memory'].check(self.agentConfig))
                metrics.extend(self._win32_system_checks['cpu'].check(self.agentConfig))
                metrics.extend(self._win32_system_checks['network'].check(self.agentConfig))
                metrics.extend(self._win32_system_checks['io'].check(self.agentConfig))
                metrics.extend(self._win32_system_checks['proc'].check(self.agentConfig))
            except Exception:
                log.exception('Unable to fetch Windows system metrics.')
        else:
            # Unix system checks
            sys_checks = self._unix_system_checks

            diskUsage = sys_checks['disk'].check(self.agentConfig)
            if diskUsage and len(diskUsage) == 2:
                payload["diskUsage"] = diskUsage[0]
                payload["inodes"] = diskUsage[1]

            load = sys_checks['load'].check(self.agentConfig)
            payload.update(load)
                
            memory = sys_checks['memory'].check(self.agentConfig)

            if memory:
                payload.update({
                    'memPhysUsed' : memory.get('physUsed'), 
                    'memPhysPctUsable' : memory.get('physPctUsable'), 
                    'memPhysFree' : memory.get('physFree'), 
                    'memPhysTotal' : memory.get('physTotal'), 
                    'memPhysUsable' : memory.get('physUsable'), 
                    'memSwapUsed' : memory.get('swapUsed'), 
                    'memSwapFree' : memory.get('swapFree'), 
                    'memSwapPctFree' : memory.get('swapPctFree'), 
                    'memSwapTotal' : memory.get('swapTotal'), 
                    'memCached' : memory.get('physCached'), 
                    'memBuffers': memory.get('physBuffers'),
                    'memShared': memory.get('physShared')
                })

            ioStats = sys_checks['io'].check(self.agentConfig)
            if ioStats:
                payload['ioStats'] = ioStats

            processes = sys_checks['processes'].check(self.agentConfig)
            payload.update({'processes': processes})

            cpuStats = sys_checks['cpu'].check(self.agentConfig)
            if cpuStats:
                payload.update(cpuStats)

        # Run old-style checks
        gangliaData = self._ganglia.check(self.agentConfig)
        dogstreamData = self._dogstream.check(self.agentConfig)
        ddforwarderData = self._ddforwarder.check(self.agentConfig)

        if gangliaData is not False and gangliaData is not None:
            payload['ganglia'] = gangliaData
           
        # dogstream
        if dogstreamData:
            dogstreamEvents = dogstreamData.get('dogstreamEvents', None)
            if dogstreamEvents:
                if 'dogstream' in payload['events']:
                    events['dogstream'].extend(dogstreamEvents)
                else:
                    events['dogstream'] = dogstreamEvents
                del dogstreamData['dogstreamEvents']

            payload.update(dogstreamData)

        # metrics about the forwarder
        if ddforwarderData:
            payload['datadog'] = ddforwarderData

        # Process the event checks. 
        for event_check in self._event_checks:
            event_data = event_check.check(log, self.agentConfig)
            if event_data:
                events[event_check.key] = event_data

        # Resources checks
        if self.os != 'windows':
            has_resource = False
            for resources_check in self._resources_checks:
                resources_check.check()
                snaps = resources_check.pop_snapshots()
                if snaps:
                    has_resource = True
                    res_value = { 'snaps': snaps,
                                  'format_version': resources_check.get_format_version() }                              
                    res_format = resources_check.describe_format_if_needed()
                    if res_format is not None:
                        res_value['format_description'] = res_format
                    payload['resources'][resources_check.RESOURCE_KEY] = res_value
     
            if has_resource:
                payload['resources']['meta'] = {
                            'api_key': self.agentConfig['api_key'],
                            'host': payload['internalHostname'],
                        }

        # newer-style checks (not checks.d style)
        for metrics_check in self._metrics_checks:
            res = metrics_check.check(self.agentConfig)
            if res:
                metrics.extend(res)

        # checks.d checks
        check_statuses = []
        for check in self.initialized_checks_d:
            if not self.continue_running:
                return
            log.info("Running check %s" % check.name)
            instance_statuses = [] 
            metric_count = 0
            event_count = 0
            try:
                # Run the check.
                instance_statuses = check.run()

                # Collect the metrics and events.
                current_check_metrics = check.get_metrics()
                current_check_events = check.get_events()

                # Save them for the payload.
                metrics.extend(current_check_metrics)
                if current_check_events:
                    if check.name not in events:
                        events[check.name] = current_check_events
                    else:
                        events[check.name] += current_check_events

                # Save the status of the check.
                metric_count = len(current_check_metrics)
                event_count = len(current_check_events)
            except Exception, e:
                log.exception("Error running check %s" % check.name)
            check_status = CheckStatus(check.name, instance_statuses, metric_count, event_count)
            check_statuses.append(check_status)

        for check_name, info in self.init_failed_checks_d.iteritems():
            if not self.continue_running:
                return
            check_status = CheckStatus(check_name, None, None, None,
                                       init_failed_error=info['error'],
                                       init_failed_traceback=info['traceback'])
            check_statuses.append(check_status)


        # Store the metrics and events in the payload.
        payload['metrics'] = metrics
        payload['events'] = events
        collect_duration = timer.step()

        if self.os != 'windows':
            payload['metrics'].extend(self._agent_metrics.check(payload, self.agentConfig, 
                collect_duration, self.emit_duration, time.clock() - cpu_clock))
        else:
            payload['metrics'].extend(self._agent_metrics.check(payload, self.agentConfig, 
                collect_duration, self.emit_duration))


        emitter_statuses = self._emit(payload)
        self.emit_duration = timer.step()

        # Persist the status of the collection run.
        try:
            CollectorStatus(check_statuses, emitter_statuses, self.metadata_cache).persist()
        except Exception:
            log.exception("Error persisting collector status")

        if self.run_count <= FLUSH_LOGGING_INITIAL or self.run_count % FLUSH_LOGGING_PERIOD == 0:
            log.info("Finished run #%s. Collection time: %ss. Emit time: %ss" %
                    (self.run_count, round(collect_duration, 2), round(self.emit_duration, 2)))
            if self.run_count == FLUSH_LOGGING_INITIAL:
                log.info("First flushes done, next flushes will be logged every %s flushes." % FLUSH_LOGGING_PERIOD)

        else:
            log.debug("Finished run #%s. Collection time: %ss. Emit time: %ss" %
                    (self.run_count, round(collect_duration, 2), round(self.emit_duration, 2)))


    def _emit(self, payload):
        """ Send the payload via the emitters. """
        statuses = []
        for emitter in self.emitters:
            # Don't try to send to an emitter if we're stopping/
            if not self.continue_running:
                return statuses
            name = emitter.__name__
            emitter_status = EmitterStatus(name)
            try:
                emitter(payload, log, self.agentConfig)
            except Exception, e:
                log.exception("Error running emitter: %s" % emitter.__name__)
                emitter_status = EmitterStatus(name, e)
            statuses.append(emitter_status)
        return statuses

    def _is_first_run(self):
        return self.run_count <= 1

    def _build_payload(self, start_event=True):
        """
        Return an dictionary that contains all of the generic payload data.
        """
        now = time.time()
        payload = {
            'collection_timestamp': now,
            'os' : self.os,
            'python': sys.version,
            'agentVersion' : self.agentConfig['version'],
            'apiKey': self.agentConfig['api_key'],
            'events': {},
            'metrics': [],
            'resources': {},
            'internalHostname' : get_hostname(self.agentConfig),
            'uuid' : get_uuid(),
        }

        # Include system stats on first postback
        if start_event and self._is_first_run():
            payload['systemStats'] = self.agentConfig.get('system_stats', {})
            # Also post an event in the newsfeed
            payload['events']['System'] = [{'api_key': self.agentConfig['api_key'],
                                 'host': payload['internalHostname'],
                                 'timestamp': now,
                                 'event_type':'Agent Startup',
                                 'msg_text': 'Version %s' % get_version()
                                 }]

        # Periodically send the host metadata.
        if self._is_first_run() or self._should_send_metadata():
            payload['systemStats'] = get_system_stats()
            payload['meta'] = self._get_metadata()
            self.metadata_cache = payload['meta']
            # Add static tags from the configuration file
            if self.agentConfig['tags'] is not None:
                payload['tags'] = self.agentConfig['tags']

            # Log the metadata on the first run
            if self._is_first_run():
                if self.agentConfig['tags'] is not None:
                    log.info(u"Hostnames: %s, tags: %s" \
                        % (repr(self.metadata_cache),
                           self.agentConfig['tags']))
                else:
                    log.info(u"Hostnames: %s" % repr(self.metadata_cache))

        return payload

    def _get_metadata(self):
        metadata = EC2.get_metadata()
        if metadata.get('hostname'):
            metadata['ec2-hostname'] = metadata.get('hostname')
            del metadata['hostname']

        if self.agentConfig.get('hostname'):
            metadata['agent-hostname'] = self.agentConfig.get('hostname')
        else:
            try:
                metadata["socket-hostname"] = socket.gethostname()
            except:
                pass
        try:
            metadata["socket-fqdn"] = socket.getfqdn()
        except:
            pass

        metadata["hostname"] = get_hostname()

        return metadata

    def _should_send_metadata(self):
        # If the interval has passed, send the metadata again
        now = time.time()
        if now - self.metadata_start >= self.metadata_interval:
            log.debug('Metadata interval has passed. Sending metadata.')
            self.metadata_start = now
            return True

        return False


