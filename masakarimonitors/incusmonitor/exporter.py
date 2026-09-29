# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Turn probe results into metrics and into a state the notifier reads.

The exporter observes and reports. It never touches an instance and it
holds no OpenStack credentials; acting on what it reports is the
notifier's job, in a container that has credentials and no privileges.
"""

import collections
import json
import threading
import time

from oslo_log import log as oslo_logging

from masakarimonitors.incusmonitor import mountinfo
from masakarimonitors.incusmonitor import promtext

LOG = oslo_logging.getLogger(__name__)

PREFIX = 'incus_exporter'
DEVICE_CHANGES_KEPT = 20


class Exporter(object):

    def __init__(self, prober, node, confirmations=3, registry=None,
                 clock=time.time):
        self._prober = prober
        self._node = node
        self._confirmations = confirmations
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot = None
        self._streaks = {}
        self._device = None
        self._device_changes = collections.deque(maxlen=DEVICE_CHANGES_KEPT)
        self.registry = registry or promtext.Registry()
        self.registry.counter(
            PREFIX + '_host_lxcfs_device_changes_total',
            'Times the device of the host LXCFS mount changed.',
            {'host': node}, amount=0)

    def sweep(self):
        started = self._clock()
        snapshot = self._prober.sweep()
        snapshot['node'] = self._node
        self._track_device(snapshot['host'])
        self._confirm(snapshot['instances'] or [])
        self._publish(snapshot, self._clock() - started)
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def _track_device(self, host):
        device = host['device']
        # Losing the mount is reported by the up gauge. Only a mount that
        # came back on another device counts as a restart.
        if device is not None:
            if self._device is not None and device != self._device:
                LOG.warning('The host LXCFS mount moved from device %s to '
                            '%s', self._device, device)
                self._device_changes.append(self._clock())
                self.registry.counter(
                    PREFIX + '_host_lxcfs_device_changes_total',
                    'Times the device of the host LXCFS mount changed.',
                    {'host': self._node})
            self._device = device
        host['device_changes'] = list(self._device_changes)

    def _confirm(self, instances):
        """Count consecutive stale sweeps of the same process.

        One stale result is not acted on. The streak belongs to the
        process, so a restarted container starts again from zero.
        """
        streaks = {}
        for instance in instances:
            key = (instance['name'], instance['pid'])
            streak = 0
            if instance['state'] == mountinfo.STALE:
                streak = self._streaks.get(key, 0) + 1
                streaks[key] = streak
            instance['stale_streak'] = streak
            instance['confirmed'] = streak >= self._confirmations
        self._streaks = streaks

    def _publish(self, snapshot, elapsed):
        registry = self.registry
        node = {'host': self._node}
        host = snapshot['host']
        instances = snapshot['instances']

        registry.gauge(PREFIX + '_host_lxcfs_up',
                       'Whether the host LXCFS mount answers a read.',
                       host['up'], node)
        registry.gauge(PREFIX + '_host_lxcfs_start_time_seconds',
                       'When the LXCFS process serving the host started.',
                       host['lxcfs_started_at'] or 0, node)
        registry.gauge(PREFIX + '_incusd_lxcfs_fresh',
                       'Whether incusd is bound to the mount the host '
                       'serves.', snapshot['incusd']['fresh'], node)
        registry.gauge(PREFIX + '_incus_api_up',
                       'Whether the instance list could be read.',
                       instances is not None, node)

        for name in (PREFIX + '_instances_state',
                     PREFIX + '_instance_lxcfs_stale',
                     PREFIX + '_instance_lxcfs_stale_confirmed'):
            registry.clear_gauge(name)
        counts = dict.fromkeys(mountinfo.STATES, 0)
        running = probed = 0
        for instance in instances or []:
            counts[instance['state']] += 1
            running += instance['running']
            if instance['state'] == mountinfo.SKIPPED:
                continue
            probed += 1
            labels = dict(node, instance=instance['name'],
                          nova_uuid=instance['nova_uuid'] or '')
            registry.gauge(PREFIX + '_instance_lxcfs_stale',
                           'Whether the instance is bound to a mount the '
                           'host no longer serves.',
                           instance['state'] == mountinfo.STALE, labels)
            registry.gauge(PREFIX + '_instance_lxcfs_stale_confirmed',
                           'Whether the instance was stale for enough '
                           'consecutive sweeps to be acted on.',
                           instance['confirmed'], labels)
        for state, count in counts.items():
            registry.gauge(PREFIX + '_instances_state',
                           'Instances by the result of the probe.',
                           count, dict(node, state=state))
        registry.gauge(PREFIX + '_instances_running',
                       'Instances that Incus reports as running.',
                       running, node)
        registry.gauge(PREFIX + '_instances_probed',
                       'Running instances that were judged in the last '
                       'sweep.', probed, node)

        for error in snapshot['errors']:
            LOG.warning('Probe error at %s: %s', error['stage'],
                        error['message'])
            registry.counter(PREFIX + '_sweep_errors_total',
                             'Errors of the probe itself.',
                             dict(node, stage=error['stage']))
        registry.observe(PREFIX + '_sweep_seconds',
                         'How long a sweep took.', elapsed, node)
        registry.gauge(PREFIX + '_last_sweep_timestamp_seconds',
                       'When the last sweep finished.',
                       self._clock(), node)

    def metrics(self):
        return promtext.CONTENT_TYPE, self.registry.render()

    def state(self):
        with self._lock:
            return 'application/json', json.dumps(self._snapshot)

    def routes(self):
        return {'/metrics': self.metrics, '/state': self.state}

    def run(self, interval, stop):
        """Sweep until the event is set."""
        while not stop.is_set():
            try:
                self.sweep()
            except Exception:
                LOG.exception('The sweep failed')
                self.registry.counter(
                    PREFIX + '_sweep_errors_total',
                    'Errors of the probe itself.',
                    {'host': self._node, 'stage': 'sweep'})
            stop.wait(interval)
