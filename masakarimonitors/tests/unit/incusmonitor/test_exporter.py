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

import json
from urllib import error
from urllib import request

import testtools

from masakarimonitors.incusmonitor import exporter
from masakarimonitors.incusmonitor import httpd
from masakarimonitors.incusmonitor import mountinfo
from masakarimonitors.incusmonitor import promtext

NODE = 'lxd-worker3.cloud.local'


def instance(name='instance-1', pid=1000, state=mountinfo.STALE,
             uuid='uuid-1'):
    return {'name': name, 'pid': pid, 'state': state, 'nova_uuid': uuid,
            'running': True, 'started_at': 1.0, 'created_at': '',
            'devices': [], 'reason': None, 'project': 'default'}


class FakeProber(object):

    def __init__(self):
        self.device = '0:231'
        self.up = True
        self.instances = []
        self.errors = []

    def sweep(self):
        return {
            'taken_at': 100.0,
            'host': {'up': self.up, 'device': self.device, 'error': None,
                     'lxcfs_pid': 900, 'lxcfs_started_at': 50.0},
            'incusd': {'pid': 500, 'fresh': True, 'device': self.device,
                       'error': None},
            'instances': self.instances,
            'errors': self.errors,
        }


class TestRegistry(testtools.TestCase):

    def test_render(self):
        registry = promtext.Registry()
        registry.gauge('up', 'Is it up.', True, {'node': 'a"b'})
        registry.counter('seen_total', 'Seen.', {'node': 'n'}, amount=0)
        registry.counter('seen_total', 'Seen.', {'node': 'n'})
        registry.gauge('when', 'When.', 1790683838.5)
        registry.observe('took', 'Took.', 0.3, buckets=(0.1, 1))

        self.assertEqual(
            '# HELP up Is it up.\n'
            '# TYPE up gauge\n'
            'up{node="a\\"b"} 1\n'
            '# HELP seen_total Seen.\n'
            '# TYPE seen_total counter\n'
            'seen_total{node="n"} 1\n'
            '# HELP when When.\n'
            '# TYPE when gauge\n'
            'when 1790683838.5\n'
            '# HELP took Took.\n'
            '# TYPE took histogram\n'
            'took_bucket{le="0.1"} 0\n'
            'took_bucket{le="1"} 1\n'
            'took_bucket{le="+Inf"} 1\n'
            'took_sum 0.3\n'
            'took_count 1\n',
            registry.render())

    def test_a_cleared_gauge_stops_reporting_old_labels(self):
        registry = promtext.Registry()
        registry.gauge('stale', 'Stale.', 1, {'instance': 'gone'})

        registry.clear_gauge('stale')
        registry.gauge('stale', 'Stale.', 1, {'instance': 'here'})

        self.assertNotIn('gone', registry.render())
        self.assertIn('here', registry.render())


class TestExporter(testtools.TestCase):

    def setUp(self):
        super(TestExporter, self).setUp()
        self.prober = FakeProber()
        self.exporter = exporter.Exporter(self.prober, NODE,
                                          confirmations=3,
                                          clock=lambda: 100.0)

    def _sweep(self):
        return self.exporter.sweep()['instances']

    def test_one_stale_sweep_confirms_nothing(self):
        self.prober.instances = [instance()]

        result = self._sweep()[0]

        self.assertEqual(1, result['stale_streak'])
        self.assertFalse(result['confirmed'])

    def test_enough_stale_sweeps_in_a_row_confirm(self):
        for _ in range(3):
            self.prober.instances = [instance()]
            result = self._sweep()[0]

        self.assertEqual(3, result['stale_streak'])
        self.assertTrue(result['confirmed'])

    def test_a_sweep_that_is_not_stale_breaks_the_streak(self):
        for state in (mountinfo.STALE, mountinfo.STALE,
                      mountinfo.SKIPPED, mountinfo.STALE):
            self.prober.instances = [instance(state=state)]
            result = self._sweep()[0]

        self.assertEqual(1, result['stale_streak'])
        self.assertFalse(result['confirmed'])

    def test_a_restarted_instance_starts_a_new_streak(self):
        for pid in (1000, 1000, 1000, 2000):
            self.prober.instances = [instance(pid=pid)]
            result = self._sweep()[0]

        self.assertEqual(1, result['stale_streak'])
        self.assertFalse(result['confirmed'])

    def test_a_moved_device_counts_as_a_restart(self):
        self.exporter.sweep()
        self.prober.device = '0:232'

        snapshot = self.exporter.sweep()

        self.assertEqual([100.0], snapshot['host']['device_changes'])
        self.assertIn(
            'incus_exporter_host_lxcfs_device_changes_total'
            '{host="%s"} 1\n' % NODE, self.exporter.metrics()[1])

    def test_a_lost_mount_is_not_a_restart(self):
        self.exporter.sweep()
        self.prober.device = None
        self.exporter.sweep()
        self.prober.device = '0:231'

        snapshot = self.exporter.sweep()

        self.assertEqual([], snapshot['host']['device_changes'])

    def test_metrics_add_up(self):
        self.prober.instances = [
            instance('a', 1, mountinfo.STALE),
            instance('b', 2, mountinfo.FRESH, uuid=None),
            instance('c', 3, mountinfo.SKIPPED),
        ]
        self.exporter.sweep()

        text = self.exporter.metrics()[1]

        for line in (
                'incus_exporter_host_lxcfs_up{host="%s"} 1',
                'incus_exporter_incusd_lxcfs_fresh{host="%s"} 1',
                'incus_exporter_incus_api_up{host="%s"} 1',
                'incus_exporter_instances_running{host="%s"} 3',
                'incus_exporter_instances_probed{host="%s"} 2',
                'incus_exporter_instances_state{host="%s",state="stale"} 1',
                'incus_exporter_instances_state{host="%s",state="fresh"} 1',
                'incus_exporter_instances_state'
                '{host="%s",state="skipped"} 1',
                'incus_exporter_instances_state'
                '{host="%s",state="unreadable"} 0',
                'incus_exporter_instance_lxcfs_stale'
                '{host="%s",instance="a",nova_uuid="uuid-1"} 1',
                'incus_exporter_instance_lxcfs_stale'
                '{host="%s",instance="b",nova_uuid=""} 0',
                'incus_exporter_last_sweep_timestamp_seconds'
                '{host="%s"} 100.0'):
            self.assertIn(line % NODE + '\n', text)
        self.assertNotIn('instance="c"', text)

    def test_a_deleted_instance_stops_being_reported(self):
        self.prober.instances = [instance('a', 1)]
        self.exporter.sweep()
        self.prober.instances = []
        self.exporter.sweep()

        self.assertNotIn('instance="a"', self.exporter.metrics()[1])

    def test_an_unreachable_incus_is_reported(self):
        self.prober.instances = None
        self.prober.errors = [{'stage': 'incus-api', 'message': 'gone'}]
        self.exporter.sweep()

        text = self.exporter.metrics()[1]

        self.assertIn('incus_exporter_incus_api_up{host="%s"} 0\n' % NODE,
                      text)
        self.assertIn('incus_exporter_sweep_errors_total'
                      '{host="%s",stage="incus-api"} 1\n' % NODE, text)

    def test_the_state_is_empty_before_the_first_sweep(self):
        self.assertEqual('null', self.exporter.state()[1])

    def test_served_over_http(self):
        self.prober.instances = [instance()]
        self.exporter.sweep()
        server = httpd.serve('127.0.0.1', 0, self.exporter.routes())
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:%d' % server.server_address[1]

        with request.urlopen(base + '/metrics') as response:
            self.assertEqual(promtext.CONTENT_TYPE,
                             response.headers['Content-Type'])
            self.assertIn(b'incus_exporter_host_lxcfs_up', response.read())
        with request.urlopen(base + '/state') as response:
            state = json.loads(response.read())
        self.assertEqual(NODE, state['node'])
        self.assertEqual('instance-1', state['instances'][0]['name'])
        self.assertRaises(error.HTTPError, request.urlopen,
                          base + '/1.0/instances')
