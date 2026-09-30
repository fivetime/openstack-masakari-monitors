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

"""A /proc on disk and an Incus that answers from a list."""

import os

from masakarimonitors.incusmonitor import incus_api

BOOT_TIME = 1000000
LIVE = '0:231'
DEAD = '0:230'
LXCFS_PATH = '/var/lib/lxcfs'

# Taken from a production compute node: a container started by an incusd
# that ran under a named label, one started by an incusd that ran as plain
# unconfined, and the two labels of incusd themselves.
CONFINED_LABEL = (
    'incus-instance-0000b23a_</var/lib/incus>//&'
    ':incus-instance-0000b23a_<var-lib-incus>:unconfined (enforce)')
STACKED_LABEL = (
    'incus-instance-0000c0da_</var/lib/incus>//&unconfined//&'
    ':incus-instance-0000c0da_<var-lib-incus>:unconfined (enforce)')
NAMED_LABEL = 'incusd-runtime (unconfined)'
PLAIN_LABEL = 'unconfined'

# Taken from a production compute node after an LXCFS restart.
HOST_MOUNTS = (
    '2508 2553 253:2 /var/lib/lxcfs /var/lib/lxcfs rw,relatime shared:1 '
    '- ext4 /dev/vda2 rw\n'
    '2553 43 %(device)s / /var/lib/lxcfs rw,nosuid,nodev,relatime '
    'shared:1321 - fuse.lxcfs lxcfs rw,user_id=0,group_id=0,allow_other\n')
INSTANCE_MOUNTS = (
    '4100 4000 252:0 / / rw,relatime - ext4 /dev/rbd0 rw\n'
    '4101 4100 %(device)s /proc/cpuinfo /proc/cpuinfo rw,nosuid,nodev '
    '- fuse.lxcfs lxcfs rw,user_id=0,group_id=0,allow_other\n'
    '4102 4100 %(device)s /proc/meminfo /proc/meminfo rw,nosuid,nodev '
    '- fuse.lxcfs lxcfs rw,user_id=0,group_id=0,allow_other\n')
PLAIN_MOUNTS = '4100 4000 252:0 / / rw,relatime - ext4 /dev/rbd0 rw\n'


class ProcTree(object):
    """Writes the files of a /proc under a temporary directory."""

    def __init__(self, root):
        self.root = root
        self._write('stat', 'cpu 0 0 0 0\nbtime %d\n' % BOOT_TIME)
        self.restricted(True)

    def _write(self, path, content):
        target = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w') as stream:
            stream.write(content)

    def restricted(self, value):
        """Set the switch, or remove it as on a kernel without it."""
        path = 'sys/kernel/apparmor_restrict_unprivileged_unconfined'
        if value is None:
            os.remove(os.path.join(self.root, path))
        else:
            self._write(path, '%d\n' % value)

    def process(self, pid, started_at, comm='init', mounts=None,
                label=None):
        ticks = int((started_at - BOOT_TIME) * os.sysconf('SC_CLK_TCK'))
        fields = ['S', '1'] + ['0'] * 17 + [str(ticks), '0']
        self._write('%s/stat' % pid,
                    '%s (%s) %s\n' % (pid, comm, ' '.join(fields)))
        self._write('%s/comm' % pid, comm + '\n')
        if mounts is not None:
            self._write('%s/mountinfo' % pid, mounts)
        if label is not None:
            self._write('%s/attr/current' % pid, label + '\n')

    def host(self, device=LIVE, readable=True):
        mounts = PLAIN_MOUNTS
        if device is not None:
            mounts = HOST_MOUNTS % {'device': device}
        self.process(1, BOOT_TIME, comm='systemd', mounts=mounts)
        if readable:
            self._write('1/root%s/proc/meminfo' % LXCFS_PATH,
                        'MemTotal: 1 kB\n')

    def incusd(self, pid=500, device=LIVE, label=NAMED_LABEL):
        self.process(pid, BOOT_TIME + 10, comm='incusd',
                     mounts=HOST_MOUNTS % {'device': device}, label=label)

    def instance(self, pid, started_at, device=LIVE, readable=True,
                 label=CONFINED_LABEL):
        mounts = PLAIN_MOUNTS
        if device is not None:
            mounts = INSTANCE_MOUNTS % {'device': device}
        self.process(pid, started_at, mounts=mounts, label=label)
        if readable:
            self._write('%s/root/proc/meminfo' % pid, 'MemTotal: 1 kB\n')


class FakeIncus(object):

    def __init__(self, server_pid=500):
        self.pid = server_pid
        self.listed = []
        self.fail = False

    def add(self, name, pid, uuid='uuid', status='Running',
            created_at='2026-09-17T05:40:02Z', kind='container'):
        self.listed.append({
            'name': name, 'project': 'default', 'type': kind,
            'status': status, 'created_at': created_at,
            'config': {'user.openstack.uuid': uuid} if uuid else {},
            'state': {'pid': pid, 'status': status},
        })

    def server_pid(self):
        return self.pid

    def instances(self, project):
        if self.fail:
            raise incus_api.IncusError('the socket is gone')
        return self.listed
