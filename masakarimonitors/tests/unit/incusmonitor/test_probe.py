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

import shutil
import tempfile

import testtools

from masakarimonitors.incusmonitor import mountinfo
from masakarimonitors.incusmonitor import probe
from masakarimonitors.incusmonitor import procfs
from masakarimonitors.tests.unit.incusmonitor import fakes

NOW = fakes.BOOT_TIME + 100000
OLD = fakes.BOOT_TIME + 500


class TestMountinfo(testtools.TestCase):

    def test_parse_keeps_what_the_probe_compares(self):
        mounts = mountinfo.parse(fakes.HOST_MOUNTS % {'device': '0:231'})

        self.assertEqual(
            [mountinfo.Mount(2508, '253:2', '/var/lib/lxcfs', 'ext4'),
             mountinfo.Mount(2553, '0:231', '/var/lib/lxcfs', 'fuse.lxcfs')],
            mounts)

    def test_parse_skips_lines_it_cannot_read(self):
        self.assertEqual([], mountinfo.parse('garbage\n\n1 2 3\n'))

    def test_parse_unescapes_the_mount_point(self):
        line = ('7 1 0:9 / /mnt/with\\040space rw - fuse.lxcfs lxcfs rw\n')

        self.assertEqual('/mnt/with space',
                         mountinfo.parse(line)[0].mount_point)

    def test_live_device_is_the_newest_mount(self):
        stacked = (fakes.HOST_MOUNTS % {'device': '0:230'} +
                   '2600 2553 0:231 / /var/lib/lxcfs rw - fuse.lxcfs '
                   'lxcfs rw\n')

        self.assertEqual('0:231', mountinfo.live_device(
            mountinfo.parse(stacked), '/var/lib/lxcfs'))

    def test_live_device_ignores_other_mount_points(self):
        mounts = mountinfo.parse(fakes.INSTANCE_MOUNTS % {'device': '0:9'})

        self.assertIsNone(mountinfo.live_device(mounts, '/var/lib/lxcfs'))

    def test_classify(self):
        cases = [
            (set(), '0:231', None, mountinfo.ABSENT),
            ({'0:231'}, None, True, mountinfo.UNKNOWN),
            ({'0:231'}, '0:231', True, mountinfo.FRESH),
            ({'0:231'}, '0:231', False, mountinfo.UNREADABLE),
            ({'0:230'}, '0:231', False, mountinfo.STALE),
            # The old daemon may still answer while it is being replaced.
            ({'0:230'}, '0:231', True, mountinfo.STALE),
            ({'0:230', '0:231'}, '0:231', True, mountinfo.STALE),
        ]
        for devices, live, readable, expected in cases:
            self.assertEqual(
                expected, mountinfo.classify(devices, live, readable),
                (devices, live, readable))


class TestProber(testtools.TestCase):

    def setUp(self):
        super(TestProber, self).setUp()
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        self.tree = fakes.ProcTree(root)
        self.incus = fakes.FakeIncus()
        self.prober = probe.Prober(
            self.incus, procfs.Proc(root, read_timeout=1),
            boot_grace=60, clock=lambda: NOW)
        self.tree.host()
        self.tree.incusd()
        self.tree.process(900, OLD + 50, comm='lxcfs')

    def _only(self, snapshot):
        self.assertEqual(1, len(snapshot['instances']))
        return snapshot['instances'][0]

    def test_a_healthy_node(self):
        self.tree.instance(1000, OLD)
        self.incus.add('instance-1', 1000, uuid='uuid-1')

        snapshot = self.prober.sweep()

        self.assertEqual([], snapshot['errors'])
        self.assertTrue(snapshot['host']['up'])
        self.assertEqual(fakes.LIVE, snapshot['host']['device'])
        self.assertEqual(900, snapshot['host']['lxcfs_pid'])
        self.assertEqual(OLD + 50, snapshot['host']['lxcfs_started_at'])
        self.assertTrue(snapshot['incusd']['fresh'])
        instance = self._only(snapshot)
        self.assertEqual(mountinfo.FRESH, instance['state'])
        self.assertEqual('uuid-1', instance['nova_uuid'])
        self.assertEqual(OLD, instance['started_at'])

    def test_an_instance_bound_before_the_restart_is_stale(self):
        self.tree.instance(1000, OLD, device=fakes.DEAD, readable=False)
        self.incus.add('instance-1', 1000)

        instance = self._only(self.prober.sweep())

        self.assertEqual(mountinfo.STALE, instance['state'])
        self.assertEqual([fakes.DEAD], instance['devices'])

    def test_a_failed_read_on_the_live_device_is_not_called_stale(self):
        self.tree.instance(1000, OLD, readable=False)
        self.incus.add('instance-1', 1000)

        instance = self._only(self.prober.sweep())

        self.assertEqual(mountinfo.UNREADABLE, instance['state'])
        self.assertIn('No such file', instance['reason'])

    def test_an_instance_without_lxcfs_is_absent(self):
        self.tree.instance(1000, OLD, device=None)
        self.incus.add('instance-1', 1000)

        self.assertEqual(mountinfo.ABSENT,
                         self._only(self.prober.sweep())['state'])

    def test_a_booting_instance_is_not_judged(self):
        self.tree.instance(1000, NOW - 30, device=fakes.DEAD)
        self.incus.add('instance-1', 1000)

        instance = self._only(self.prober.sweep())

        self.assertEqual(mountinfo.SKIPPED, instance['state'])
        self.assertIn('booting', instance['reason'])

    def test_a_stopped_instance_is_not_judged(self):
        self.incus.add('instance-1', 0, status='Stopped')

        instance = self._only(self.prober.sweep())

        self.assertEqual(mountinfo.SKIPPED, instance['state'])
        self.assertFalse(instance['running'])

    def test_an_instance_whose_process_is_gone_is_not_judged(self):
        self.incus.add('instance-1', 4242)

        self.assertEqual(mountinfo.SKIPPED,
                         self._only(self.prober.sweep())['state'])

    def test_virtual_machines_are_left_out(self):
        self.incus.add('vm-1', 1000, kind='virtual-machine')

        self.assertEqual([], self.prober.sweep()['instances'])

    def test_instances_are_ordered_by_age(self):
        for name, pid, created in (('b', 1001, '2026-09-18T00:00:00Z'),
                                   ('a', 1002, '2026-09-19T00:00:00Z'),
                                   ('c', 1003, '2026-09-17T00:00:00Z')):
            self.tree.instance(pid, OLD)
            self.incus.add(name, pid, created_at=created)

        names = [instance['name']
                 for instance in self.prober.sweep()['instances']]

        self.assertEqual(['c', 'b', 'a'], names)

    def test_a_host_without_lxcfs(self):
        self.tree.host(device=None)
        self.tree.instance(1000, OLD, device=fakes.DEAD)
        self.incus.add('instance-1', 1000)

        snapshot = self.prober.sweep()

        self.assertFalse(snapshot['host']['up'])
        self.assertIsNone(snapshot['host']['device'])
        self.assertFalse(snapshot['incusd']['fresh'])
        self.assertEqual(mountinfo.UNKNOWN,
                         self._only(snapshot)['state'])

    def test_a_host_mount_that_does_not_answer(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        tree = fakes.ProcTree(root)
        tree.host(readable=False)
        tree.incusd()
        prober = probe.Prober(self.incus, procfs.Proc(root, 1),
                              clock=lambda: NOW)

        host = prober.sweep()['host']

        self.assertFalse(host['up'])
        self.assertEqual(fakes.LIVE, host['device'])

    def test_a_stale_incusd(self):
        self.tree.incusd(device=fakes.DEAD)

        incusd = self.prober.sweep()['incusd']

        self.assertFalse(incusd['fresh'])
        self.assertEqual(fakes.DEAD, incusd['device'])

    def test_an_incusd_that_cannot_be_inspected(self):
        self.incus.pid = 4242

        snapshot = self.prober.sweep()

        self.assertFalse(snapshot['incusd']['fresh'])
        self.assertEqual(['incusd'],
                         [error['stage'] for error in snapshot['errors']])

    def test_an_unreachable_incus_is_not_an_empty_node(self):
        self.incus.fail = True

        snapshot = self.prober.sweep()

        self.assertIsNone(snapshot['instances'])
        self.assertEqual(['incus-api'],
                         [error['stage'] for error in snapshot['errors']])
        self.assertTrue(snapshot['host']['up'])

    def test_the_newest_lxcfs_process_names_the_generation(self):
        self.tree.process(901, OLD + 900, comm='lxcfs')

        host = self.prober.sweep()['host']

        self.assertEqual(901, host['lxcfs_pid'])
        self.assertEqual(OLD + 900, host['lxcfs_started_at'])

    def test_a_replaced_lxcfs_process_is_found_again(self):
        self.prober.sweep()
        shutil.rmtree(self.tree.root + '/900')
        self.tree.process(950, OLD + 2000, comm='lxcfs')

        host = self.prober.sweep()['host']

        self.assertEqual(950, host['lxcfs_pid'])
        self.assertEqual(OLD + 2000, host['lxcfs_started_at'])
