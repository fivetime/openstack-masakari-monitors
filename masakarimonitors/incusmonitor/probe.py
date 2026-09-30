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

"""Probe the three places an LXCFS mount can go stale.

The host serves the mount, incusd binds it for the containers it starts,
and every running container holds the binding it was started with. Each
layer is recovered differently, so each is judged on its own:

* a dead host mount is a node incident;
* a stale incusd needs its pod replaced, and until then a restarted
  container would only bind the dead mount again;
* a stale container needs to be stopped and started.

The AppArmor label of a container is settled the same way, at its start
and from the state of the incusd that starts it, so the same sweep reads
it at the same three places.
"""

import time

from masakarimonitors.incusmonitor import apparmor
from masakarimonitors.incusmonitor import incus_api
from masakarimonitors.incusmonitor import mountinfo

NOVA_UUID_KEY = 'user.openstack.uuid'
LXCFS_COMM = 'lxcfs'


class Prober(object):

    def __init__(self, incus, proc, lxcfs_path='/var/lib/lxcfs',
                 project='default', boot_grace=60, clock=time.time):
        self._incus = incus
        self._proc = proc
        self._lxcfs_path = lxcfs_path
        self._project = project
        self._boot_grace = boot_grace
        self._clock = clock
        self._lxcfs_pid = None

    def sweep(self):
        """Probe every layer once and return what was found."""
        errors = []
        host = self._probe_host(errors)
        return {
            'taken_at': self._clock(),
            'host': host,
            'incusd': self._probe_incusd(host, errors),
            'instances': self._probe_instances(host['device'], errors),
            'errors': errors,
        }

    def _probe_host(self, errors):
        host = {'up': False, 'device': None, 'error': None,
                'lxcfs_pid': None, 'lxcfs_started_at': None,
                'apparmor_restricted': None}
        try:
            host['apparmor_restricted'] = self._proc.flag(
                *apparmor.RESTRICTION)
        except OSError as exc:
            errors.append({'stage': 'host',
                           'message': 'cannot read the AppArmor '
                                      'restriction: %s' % exc})
        try:
            mounts = mountinfo.parse(self._proc.mountinfo(1))
        except OSError as exc:
            host['error'] = 'cannot read the host mounts: %s' % exc
            errors.append({'stage': 'host', 'message': host['error']})
            return host

        host['device'] = mountinfo.live_device(mounts, self._lxcfs_path)
        if host['device'] is None:
            host['error'] = 'LXCFS is not mounted at %s' % self._lxcfs_path
            return host

        host['up'], host['error'] = self._proc.readable(
            1, 'root', self._lxcfs_path.lstrip('/'), 'proc', 'meminfo')
        host['lxcfs_pid'], host['lxcfs_started_at'] = self._find_lxcfs()
        return host

    def _find_lxcfs(self):
        """Return the LXCFS process and when it started.

        The start time names the current generation of the mount. It does
        not change while the process lives and it is known again after the
        monitor itself restarts, which a device number seen earlier is not.
        """
        started_at = None
        if self._lxcfs_pid is not None:
            started_at = self._proc.start_time(self._lxcfs_pid)
        if started_at is None:
            self._lxcfs_pid = None
            # More than one match means an old process has not exited yet.
            # The newest one is the one serving the live mount.
            for pid in self._proc.find_by_comm(LXCFS_COMM):
                candidate = self._proc.start_time(pid)
                if candidate is not None and (started_at is None or
                                              candidate > started_at):
                    self._lxcfs_pid, started_at = pid, candidate
        return self._lxcfs_pid, started_at

    def _probe_incusd(self, host, errors):
        live = host['device']
        # Until its label is known, incusd is taken to start containers
        # wrongly: a restart asked for on a guess could ruin the container.
        incusd = {'pid': None, 'fresh': False, 'device': None, 'error': None,
                  'label': None, 'launches_stacked': True}
        try:
            incusd['pid'] = self._incus.server_pid()
            mounts = mountinfo.parse(self._proc.mountinfo(incusd['pid']))
            incusd['label'] = self._proc.label(incusd['pid'])
        except (incus_api.IncusError, OSError, KeyError, TypeError) as exc:
            incusd['error'] = 'cannot inspect incusd: %s' % exc
            errors.append({'stage': 'incusd', 'message': incusd['error']})
            return incusd

        incusd['launches_stacked'] = apparmor.launches_stacked(
            incusd['label'], host['apparmor_restricted'])
        incusd['device'] = mountinfo.live_device(mounts, self._lxcfs_path)
        if incusd['device'] is None:
            incusd['error'] = ('incusd has no LXCFS mount at %s'
                               % self._lxcfs_path)
        elif live is not None:
            incusd['fresh'] = incusd['device'] == live
        return incusd

    def _probe_instances(self, live, errors):
        try:
            listed = self._incus.instances(self._project)
        except incus_api.IncusError as exc:
            errors.append({'stage': 'incus-api', 'message': str(exc)})
            return None
        instances = [self._probe_instance(entry, live)
                     for entry in listed or []
                     if entry.get('type') == 'container']
        return sorted(instances,
                      key=lambda entry: (entry['created_at'], entry['name']))

    def _probe_instance(self, entry, live):
        state = entry.get('state') or {}
        instance = {
            'name': entry.get('name'),
            'project': entry.get('project') or self._project,
            'nova_uuid': (entry.get('config') or {}).get(NOVA_UUID_KEY),
            'created_at': entry.get('created_at') or '',
            'running': entry.get('status') == 'Running',
            'pid': state.get('pid') or None,
            'started_at': None,
            'state': mountinfo.SKIPPED,
            'devices': [],
            'reason': None,
            'confinement': apparmor.SKIPPED,
            'label': None,
        }
        if not instance['running']:
            instance['reason'] = 'the instance is not running'
            return instance
        pid = instance['pid']
        if pid is None:
            instance['reason'] = 'Incus reports no process'
            return instance
        instance['started_at'] = self._proc.start_time(pid)
        if instance['started_at'] is None:
            instance['reason'] = 'the process is gone'
            return instance
        if self._clock() - instance['started_at'] < self._boot_grace:
            instance['reason'] = 'the instance is still booting'
            return instance

        try:
            instance['label'] = self._proc.label(pid)
        except OSError:
            pass
        instance['confinement'] = apparmor.classify(instance['label'])

        try:
            devices = mountinfo.lxcfs_devices(
                mountinfo.parse(self._proc.mountinfo(pid)))
        except OSError as exc:
            instance['state'] = mountinfo.UNKNOWN
            instance['reason'] = 'cannot read the mounts: %s' % exc
            return instance
        instance['devices'] = sorted(devices)

        readable = None
        if devices:
            readable, instance['reason'] = self._proc.readable(
                pid, 'root', 'proc', 'meminfo')
        instance['state'] = mountinfo.classify(devices, live, readable)

        # A container restarted halfway through was judged from a mixture
        # of its old and new processes, so its result means nothing.
        if self._proc.start_time(pid) != instance['started_at']:
            instance['state'] = mountinfo.SKIPPED
            instance['confinement'] = apparmor.SKIPPED
            instance['reason'] = 'the instance restarted during the probe'
        return instance
