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

import datetime
from unittest import mock

from openstack import exceptions
import testtools

from masakarimonitors.incusmonitor import mountinfo
from masakarimonitors.incusmonitor import notifier

HOST = 'lxd-worker3.cloud.local'
LXCFS_STARTED = 1000.0
INSTANCE_STARTED = 500.0
NOW = 5000.0


def instance(name, uuid, state=mountinfo.STALE, confirmed=True,
             started_at=INSTANCE_STARTED):
    return {'name': name, 'nova_uuid': uuid, 'state': state,
            'confirmed': confirmed, 'started_at': started_at,
            'pid': 1, 'running': True}


class FakeMasakari(object):
    """Keeps the notifications as Masakari would, across notifiers."""

    def __init__(self):
        self.stored = []
        self.result = notifier.ACCEPTED
        self.fail_listing = False

    def create(self, hostname, generated_at, payload):
        if self.result != notifier.ACCEPTED:
            return self.result, 'refused'
        uuid = 'notification-%d' % len(self.stored)
        self.stored.append({'uuid': uuid, 'status': 'new',
                            'payload': payload, 'hostname': hostname,
                            'generated_at': generated_at,
                            'updated_at': generated_at})
        return notifier.ACCEPTED, uuid

    def records(self, since):
        if self.fail_listing:
            raise RuntimeError('the API is down')
        return [record for record in self.stored
                if record['generated_at'] >= since]

    def settle(self, status, at, index=-1):
        self.stored[index].update(status=status, updated_at=at)

    def sent_for(self):
        return [record['payload']['instance_uuid']
                for record in self.stored]


class TestNotifier(testtools.TestCase):

    def setUp(self):
        super(TestNotifier, self).setUp()
        self.now = NOW
        self.masakari = FakeMasakari()
        self.instances = [instance('a', 'uuid-a'), instance('b', 'uuid-b')]
        self.host = {'up': True, 'device': '0:231',
                     'lxcfs_started_at': LXCFS_STARTED, 'device_changes': []}
        self.incusd = {'fresh': True, 'launches_stacked': False}
        self.taken_at = None
        self.notifier = self._notifier()

    def _notifier(self, **kwargs):
        return notifier.Notifier(self._state, self.masakari, HOST,
                                 clock=lambda: self.now, **kwargs)

    def _state(self):
        taken_at = self.now if self.taken_at is None else self.taken_at
        return {'taken_at': taken_at, 'host': self.host,
                'incusd': self.incusd, 'instances': self.instances}

    def _step(self, advance=0):
        self.now += advance
        self.notifier.step()

    def _metrics(self):
        return self.notifier.metrics()[1]

    def _blocked(self, reason):
        return ('incus_notifier_autoheal_blocked{host="%s",reason="%s"} 1\n'
                % (HOST, reason)) in self._metrics()

    def _restart(self, name, at):
        for entry in self.instances:
            if entry['name'] == name:
                entry.update(started_at=at, state=mountinfo.SKIPPED,
                             confirmed=False)

    def _recover(self, name):
        for entry in self.instances:
            if entry['name'] == name:
                entry.update(state=mountinfo.FRESH, confirmed=False)

    def test_notifies_for_the_oldest_stale_instance(self):
        self._step()

        self.assertEqual(
            [{'event': 'INCUS_GUEST_ERROR', 'instance_uuid': 'uuid-a',
              'vir_domain_event': 'LXCFS_STALE'}],
            [record['payload'] for record in self.masakari.stored])
        self.assertEqual(HOST, self.masakari.stored[0]['hostname'])
        self.assertIn('incus_notifier_notifications_total'
                      '{host="%s",result="accepted"} 1\n' % HOST,
                      self._metrics())

    def test_nothing_to_do_on_a_healthy_node(self):
        self.instances = [instance('a', 'uuid-a', mountinfo.FRESH, False)]

        self._step()

        self.assertEqual([], self.masakari.stored)
        self.assertNotIn('} 1\n', self._metrics().split(
            'incus_notifier_autoheal_blocked', 1)[1].split('# HELP')[0])

    def test_an_unconfirmed_instance_waits(self):
        self.instances = [instance('a', 'uuid-a', confirmed=False)]

        self._step()

        self.assertEqual([], self.masakari.stored)

    def test_an_unreadable_instance_is_never_restarted(self):
        self.instances = [instance('a', 'uuid-a', mountinfo.UNREADABLE,
                                   confirmed=False)]

        self._step()

        self.assertEqual([], self.masakari.stored)

    def test_an_instance_nova_does_not_know_waits_for_a_human(self):
        self.instances = [instance('a', None)]

        self._step()

        self.assertEqual([], self.masakari.stored)
        self.assertIn('incus_notifier_instance_awaiting_manual'
                      '{host="%s",incus_instance="a",nova_uuid=""} 1\n' % HOST,
                      self._metrics())

    def test_one_recovery_at_a_time(self):
        self._step()
        self._step(advance=15)
        self.masakari.settle('running', self.now)
        self._step(advance=15)

        self.assertEqual(['uuid-a'], self.masakari.sent_for())
        self.assertIn('incus_notifier_recovery_in_flight{host="%s"} 1\n'
                      % HOST, self._metrics())

    def test_the_next_one_follows_a_verified_recovery(self):
        self._step()
        self._restart('a', self.now + 5)
        self.masakari.settle('finished', self.now + 20)

        # Finished, but the restarted instance has not been judged yet.
        self._step(advance=40)
        self.assertEqual(['uuid-a'], self.masakari.sent_for())

        self._recover('a')
        self._step(advance=30)

        self.assertEqual(['uuid-a', 'uuid-b'], self.masakari.sent_for())
        self.assertIn('incus_notifier_recoveries_total'
                      '{host="%s",result="recovered"} 1\n' % HOST,
                      self._metrics())
        self.assertIn('incus_notifier_recovery_seconds_count'
                      '{host="%s"} 1\n' % HOST, self._metrics())

    def test_the_gap_after_a_recovery_is_kept(self):
        self._step()
        self._restart('a', self.now + 5)
        self._recover('a')
        self.masakari.settle('finished', self.now + 20)

        self._step(advance=25)
        self.assertEqual(['uuid-a'], self.masakari.sent_for())

        self._step(advance=30)
        self.assertEqual(['uuid-a', 'uuid-b'], self.masakari.sent_for())

    def test_a_skipped_instance_is_not_asked_for_again(self):
        self._step()
        # Finished without a restart: the instance is not HA enabled.
        self.masakari.settle('finished', self.now + 1)

        self._step(advance=40)
        self._step(advance=40)
        self._step(advance=40)

        self.assertEqual(['uuid-a', 'uuid-b'], self.masakari.sent_for())
        self.assertIn('incus_notifier_recoveries_total'
                      '{host="%s",result="skipped"} 1\n' % HOST,
                      self._metrics())
        self.assertIn('incus_notifier_instance_awaiting_manual'
                      '{host="%s",incus_instance="a",nova_uuid="uuid-a"} 1\n'
                      % HOST, self._metrics())

    def test_a_restarted_notifier_does_not_ask_twice(self):
        self._step()
        self.masakari.settle('finished', self.now + 1)
        self.instances = [instance('a', 'uuid-a')]

        self.notifier = self._notifier()
        self._step(advance=600)

        self.assertEqual(['uuid-a'], self.masakari.sent_for())

    def test_a_new_generation_is_asked_for_again(self):
        self._step()
        self.masakari.settle('finished', self.now + 1)
        self.instances = [instance('a', 'uuid-a')]

        self.host['lxcfs_started_at'] = self.now + 100
        self._step(advance=600)

        self.assertEqual(['uuid-a', 'uuid-a'], self.masakari.sent_for())

    def test_a_recovery_that_changes_nothing_stops_the_node(self):
        self._step()
        self._restart('a', self.now + 5)
        self.masakari.settle('finished', self.now + 20)
        self._step(advance=40)
        for entry in self.instances:
            if entry['name'] == 'a':
                entry.update(state=mountinfo.STALE, confirmed=True)

        self._step(advance=60)
        self._step(advance=60)

        self.assertEqual(['uuid-a'], self.masakari.sent_for())
        self.assertTrue(self._blocked(notifier.RECOVERY_INEFFECTIVE))
        self.assertIn('incus_notifier_recoveries_total'
                      '{host="%s",result="ineffective"} 1\n' % HOST,
                      self._metrics())

    def test_repeated_failures_stop_the_node_for_a_while(self):
        self.instances.append(instance('c', 'uuid-c'))
        self._step()
        self.masakari.settle('error', self.now + 10)
        self._step(advance=60)
        self.masakari.settle('failed', self.now + 10)

        self._step(advance=60)
        self.assertEqual(['uuid-a', 'uuid-b'], self.masakari.sent_for())
        self.assertTrue(self._blocked(notifier.RECOVERY_ERRORS))

        self._step(advance=3600)
        self.assertEqual(['uuid-a', 'uuid-b', 'uuid-c'],
                         self.masakari.sent_for())

    def test_a_notification_nobody_finishes_times_out(self):
        self._step()

        self._step(advance=301)

        self.assertIn('incus_notifier_recoveries_total'
                      '{host="%s",result="timeout"} 1\n' % HOST,
                      self._metrics())
        self.assertEqual(['uuid-a', 'uuid-b'], self.masakari.sent_for())

    def test_an_ignored_notification_waits_for_a_human(self):
        self._step()
        self.masakari.settle('ignored', self.now + 1)

        self._step(advance=40)

        self.assertIn('incus_notifier_recoveries_total'
                      '{host="%s",result="ignored"} 1\n' % HOST,
                      self._metrics())
        self.assertIn('incus_instance="a"', self._metrics())

    def test_a_rejection_stops_the_node_for_a_while(self):
        self.masakari.result = notifier.REJECTED
        self._step()
        self.masakari.result = notifier.ACCEPTED

        self._step(advance=15)
        self.assertEqual([], self.masakari.stored)
        self.assertTrue(self._blocked(notifier.NOTIFICATION_REJECTED))

        self._step(advance=600)
        self.assertEqual(['uuid-a'], self.masakari.sent_for())

    def test_blocked_while_the_host_mount_is_down(self):
        self.host['up'] = False

        self._step()

        self.assertEqual([], self.masakari.stored)
        self.assertTrue(self._blocked(notifier.HOST_LXCFS_DOWN))

    def test_blocked_while_incusd_is_stale(self):
        self.incusd['fresh'] = False

        self._step()

        self.assertEqual([], self.masakari.stored)
        self.assertTrue(self._blocked(notifier.INCUSD_VIEW_STALE))

    def test_an_unmatched_recovery_still_holds_back_the_next(self):
        # Found on production: while an instance stops, Incus can list it
        # without its configuration, so its Nova UUID is missing and its
        # record matches no instance in the snapshot.
        self._step()
        self.assertEqual(['uuid-a'], self.masakari.sent_for())
        self.instances[0].update(nova_uuid=None, state=mountinfo.SKIPPED,
                                 confirmed=False, running=False)

        self._step(advance=60)

        self.assertEqual(['uuid-a'], self.masakari.sent_for())
        self.assertIn('incus_notifier_recovery_in_flight'
                      '{host="%s"} 1\n' % HOST, self._metrics())

    def test_an_unmatched_recovery_still_spaces_the_next(self):
        self._step()
        self.instances[0].update(nova_uuid=None, state=mountinfo.SKIPPED,
                                 confirmed=False, running=False)
        self.masakari.settle('finished', self.now + 50)

        self._step(advance=60)

        self.assertEqual(['uuid-a'], self.masakari.sent_for())

        self._step(advance=30)

        self.assertEqual(['uuid-a', 'uuid-b'], self.masakari.sent_for())

    def test_blocked_while_a_restart_would_come_up_stacked(self):
        self.incusd['launches_stacked'] = True

        self._step()

        self.assertEqual([], self.masakari.stored)
        self.assertTrue(self._blocked(notifier.INCUSD_LAUNCHES_STACKED))

    def test_blocked_without_a_generation_to_key_on(self):
        self.host['lxcfs_started_at'] = None

        self._step()

        self.assertEqual([], self.masakari.stored)
        self.assertTrue(self._blocked(notifier.LXCFS_PROCESS_UNKNOWN))

    def test_blocked_while_lxcfs_keeps_restarting(self):
        self.host['device_changes'] = [self.now - 500, self.now - 10]

        self._step()

        self.assertEqual([], self.masakari.stored)
        self.assertTrue(self._blocked(notifier.LXCFS_FLAPPING))

    def test_old_restarts_do_not_count_as_flapping(self):
        self.host['device_changes'] = [self.now - 5000, self.now - 10]

        self._step()

        self.assertEqual(['uuid-a'], self.masakari.sent_for())

    def test_blocked_when_the_instance_list_is_unknown(self):
        self.instances = None

        self._step()

        self.assertTrue(self._blocked(notifier.INCUS_API_UNAVAILABLE))

    def test_blocked_when_the_exporter_does_not_answer(self):
        self.notifier = notifier.Notifier(
            mock.Mock(side_effect=OSError('refused')), self.masakari,
            HOST, clock=lambda: self.now)

        self._step()

        self.assertEqual([], self.masakari.stored)
        self.assertTrue(self._blocked(notifier.EXPORTER_UNAVAILABLE))

    def test_blocked_before_the_first_sweep(self):
        self.notifier = notifier.Notifier(
            lambda: None, self.masakari, HOST, clock=lambda: self.now)

        self._step()

        self.assertTrue(self._blocked(notifier.EXPORTER_UNAVAILABLE))

    def test_blocked_when_the_state_is_old(self):
        self.taken_at = self.now - 61

        self._step()

        self.assertEqual([], self.masakari.stored)
        self.assertTrue(self._blocked(notifier.EXPORTER_UNAVAILABLE))

    def test_blocked_when_the_records_cannot_be_read(self):
        self.masakari.fail_listing = True

        self._step()

        self.assertEqual([], self.masakari.stored)
        self.assertTrue(self._blocked(notifier.MASAKARI_UNAVAILABLE))

    def test_notifications_of_other_monitors_are_not_ours(self):
        self.masakari.stored.append({
            'uuid': 'libvirt', 'status': 'finished',
            'payload': {'event': 'LIFECYCLE', 'instance_uuid': 'uuid-a',
                        'vir_domain_event': 'STOPPED_FAILED'},
            'generated_at': self.now - 100, 'updated_at': self.now - 90})

        self._step()

        self.assertEqual(['uuid-a', 'uuid-a'], self.masakari.sent_for())

    def test_a_dry_run_sends_nothing(self):
        self.notifier = self._notifier(dry_run=True)

        self._step()
        self._step(advance=60)

        self.assertEqual([], self.masakari.stored)
        self.assertIn('incus_notifier_notifications_total'
                      '{host="%s",result="dry-run"} 1\n' % HOST,
                      self._metrics())


class TestDryRunFor(testtools.TestCase):

    def test_every_host_is_armed_by_default(self):
        self.assertEqual((False, None),
                         notifier.dry_run_for(HOST, False, []))

    def test_an_armed_host_sends(self):
        self.assertEqual((False, None), notifier.dry_run_for(
            HOST, False, ['other.cloud.local', HOST]))

    def test_a_host_left_out_only_logs(self):
        dry_run, why = notifier.dry_run_for(
            HOST, False, ['other.cloud.local'])

        self.assertTrue(dry_run)
        self.assertIn(HOST, why)

    def test_dry_run_wins_over_the_list(self):
        self.assertTrue(notifier.dry_run_for(HOST, True, [HOST])[0])

    def test_names_must_match_exactly(self):
        # A short name is not the name the host notifies as.
        self.assertTrue(notifier.dry_run_for(
            HOST, False, [HOST.split('.')[0]])[0])


class TestMasakari(testtools.TestCase):

    def setUp(self):
        super(TestMasakari, self).setUp()
        self.sender = mock.Mock()
        self.client = self.sender.masakari_client
        self.masakari = notifier.Masakari(self.sender)
        self.payload = {'event': 'INCUS_GUEST_ERROR',
                        'instance_uuid': 'uuid-a',
                        'vir_domain_event': 'LXCFS_STALE'}

    def test_create_sends_naive_utc(self):
        self.client.create_notification.return_value = mock.Mock(
            notification_uuid='n-1')

        result = self.masakari.create(HOST, 1790683838.0, self.payload)

        self.assertEqual((notifier.ACCEPTED, 'n-1'), result)
        self.client.create_notification.assert_called_once_with(
            type='VM', hostname=HOST, payload=self.payload,
            generated_time=datetime.datetime(2026, 9, 29, 12, 10, 38))

    def test_create_names_what_went_wrong(self):
        for status, expected in ((400, notifier.REJECTED),
                                 (409, notifier.DUPLICATE),
                                 (503, notifier.ERROR)):
            self.client.create_notification.side_effect = (
                exceptions.HttpException(http_status=status))

            self.assertEqual(
                expected,
                self.masakari.create(HOST, NOW, self.payload)[0])

    def test_create_survives_a_lost_connection(self):
        self.client.create_notification.side_effect = OSError('gone')

        self.assertEqual(
            notifier.ERROR,
            self.masakari.create(HOST, NOW, self.payload)[0])
        self.assertIsNone(self.sender._masakari_client)

    def test_records_are_read_since_the_generation_started(self):
        self.client.notifications.return_value = [mock.Mock(
            notification_uuid='n-1', status='finished',
            payload=self.payload,
            generated_time='2026-09-29T12:10:38.000000',
            updated_at='2026-09-29T12:11:00.000000')]

        records = self.masakari.records(1790683000.0)

        self.client.notifications.assert_called_once_with(
            type='VM', generated_since='2026-09-29T11:56:40')
        self.assertEqual(
            [{'uuid': 'n-1', 'status': 'finished', 'payload': self.payload,
              'generated_at': 1790683838.0, 'updated_at': 1790683860.0}],
            records)
