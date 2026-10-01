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

from unittest import mock

from oslo_config import fixture as config_fixture
import testtools

from masakarimonitors.cmd import incusnotifier
import masakarimonitors.conf

HOST = 'lxd-worker3.cloud.local'


class TestBuildNotifier(testtools.TestCase):

    def setUp(self):
        super(TestBuildNotifier, self).setUp()
        self.conf = self.useFixture(
            config_fixture.Config(masakarimonitors.conf.CONF))
        self.conf.config(hostname=HOST)
        mock.patch.object(
            incusnotifier.notifier, 'Masakari').start()
        self.addCleanup(mock.patch.stopall)

    def _dry_run(self):
        return incusnotifier.build()._dry_run

    def test_sends_when_no_host_is_listed(self):
        self.assertFalse(self._dry_run())

    def test_sends_from_a_listed_host(self):
        self.conf.config(armed_hosts=[HOST], group='incus_notifier')

        self.assertFalse(self._dry_run())

    def test_only_logs_from_a_host_left_out(self):
        self.conf.config(armed_hosts=['lxd-worker1.cloud.local'],
                         group='incus_notifier')

        self.assertTrue(self._dry_run())
