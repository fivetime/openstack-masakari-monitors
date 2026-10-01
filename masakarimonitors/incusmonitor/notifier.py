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

"""Ask Masakari to recover instances whose LXCFS mount went stale.

The notifier decides nothing about an instance's right to be recovered:
that is the engine's decision, taken from the instance's metadata. What it
decides is whether asking is safe at all, and how fast to ask.

An LXCFS restart makes every container on the node stale at once, so the
whole node failing together is the normal case here and not a reason to
stop. Recovery is paced instead, one instance at a time.

Nothing about an instance is remembered between loops. What was already
asked for is read back from Masakari, and whether it happened is read from
the instance's start time, so a restarted notifier picks up where it was
and never asks twice for the same generation of the mount.
"""

import datetime
import json
import time
from urllib import request

from openstack import exceptions
from oslo_log import log as oslo_logging

from masakarimonitors.ha import masakari
from masakarimonitors.incusmonitor import mountinfo
from masakarimonitors.incusmonitor import promtext

LOG = oslo_logging.getLogger(__name__)

PREFIX = 'incus_notifier'
NOTIFICATION_TYPE = 'VM'

# What became of a request to send a notification.
ACCEPTED = 'accepted'
DUPLICATE = 'duplicate'
REJECTED = 'rejected'
ERROR = 'error'
DRY_RUN = 'dry-run'
SEND_RESULTS = (ACCEPTED, DUPLICATE, REJECTED, ERROR, DRY_RUN)

# What became of an instance that a notification was sent for.
CANDIDATE = 'candidate'
IN_FLIGHT = 'in-flight'
RECOVERED = 'recovered'
SKIPPED = 'skipped'
IGNORED = 'ignored'
INEFFECTIVE = 'ineffective'
FAILED = 'error'
TIMEOUT = 'timeout'
OUTCOMES = (RECOVERED, SKIPPED, IGNORED, INEFFECTIVE, FAILED, TIMEOUT)
FAILURES = (FAILED, TIMEOUT)

# Why no notification may be sent from this node.
EXPORTER_UNAVAILABLE = 'exporter-unavailable'
INCUS_API_UNAVAILABLE = 'incus-api-unavailable'
MASAKARI_UNAVAILABLE = 'masakari-unavailable'
HOST_LXCFS_DOWN = 'host-lxcfs-down'
LXCFS_PROCESS_UNKNOWN = 'lxcfs-process-unknown'
INCUSD_VIEW_STALE = 'incusd-view-stale'
INCUSD_LAUNCHES_STACKED = 'incusd-launches-stacked'
LXCFS_FLAPPING = 'lxcfs-flapping'
RECOVERY_INEFFECTIVE = 'recovery-ineffective'
RECOVERY_ERRORS = 'recovery-errors'
NOTIFICATION_REJECTED = 'notification-rejected'
BLOCK_REASONS = (
    EXPORTER_UNAVAILABLE, INCUS_API_UNAVAILABLE, MASAKARI_UNAVAILABLE,
    HOST_LXCFS_DOWN, LXCFS_PROCESS_UNKNOWN, INCUSD_VIEW_STALE,
    INCUSD_LAUNCHES_STACKED, LXCFS_FLAPPING, RECOVERY_INEFFECTIVE,
    RECOVERY_ERRORS, NOTIFICATION_REJECTED)


def dry_run_for(hostname, dry_run, armed_hosts):
    """Return whether the notifier on this host only logs.

    :returns: a tuple of the answer and why, for the log.
    """
    if dry_run:
        return True, 'dry_run is set'
    if armed_hosts and hostname not in armed_hosts:
        return True, '%s is not one of the armed hosts' % hostname
    return False, None


def fetch_state(url, timeout=5):
    """Return the exporter's last snapshot, or None before its first."""
    with request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def _epoch(value):
    """Return a Masakari timestamp, which is naive UTC, as epoch seconds."""
    if not value:
        return None
    if isinstance(value, str):
        value = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.timestamp()


def _naive_utc(epoch):
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).replace(tzinfo=None)


class Masakari(object):
    """The Masakari calls the notifier makes, with their results named."""

    def __init__(self, sender=None):
        self._sender = sender or masakari.SendNotification()

    def create(self, hostname, generated_at, payload):
        """Send one notification.

        :returns: a tuple of the result and a detail, which is the ID of
            the notification when it was accepted and the error otherwise.
        """
        try:
            response = self._sender.masakari_client.create_notification(
                type=NOTIFICATION_TYPE, hostname=hostname,
                generated_time=_naive_utc(generated_at), payload=payload)
            return ACCEPTED, response.notification_uuid
        except exceptions.HttpException as exc:
            if exc.status_code == 409:
                return DUPLICATE, str(exc)
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                return REJECTED, str(exc)
            return ERROR, str(exc)
        except Exception as exc:
            self._sender._masakari_client = None
            return ERROR, str(exc)

    def records(self, since):
        """Return the notifications generated since the given time."""
        found = []
        listing = self._sender.masakari_client.notifications(
            type=NOTIFICATION_TYPE,
            generated_since=_naive_utc(since).isoformat(
                timespec='seconds'))
        for entry in listing:
            found.append({
                'uuid': entry.notification_uuid,
                'status': entry.status,
                'payload': entry.payload or {},
                'generated_at': _epoch(entry.generated_time),
                'updated_at': _epoch(entry.updated_at),
            })
        return found


class Notifier(object):

    def __init__(self, fetch, masakari_api, hostname, registry=None,
                 event='INCUS_GUEST_ERROR', detail='LXCFS_STALE',
                 min_gap=30, recovery_timeout=300, max_state_age=60,
                 flap_window=600, flap_threshold=2, error_threshold=2,
                 error_cooldown=3600, reject_cooldown=600, dry_run=False,
                 clock=time.time):
        self._fetch = fetch
        self._masakari = masakari_api
        self._hostname = hostname
        self._event = event
        self._detail = detail
        self._min_gap = min_gap
        self._recovery_timeout = recovery_timeout
        self._max_state_age = max_state_age
        self._flap_window = flap_window
        self._flap_threshold = flap_threshold
        self._error_threshold = error_threshold
        self._error_cooldown = error_cooldown
        self._reject_cooldown = reject_cooldown
        self._dry_run = dry_run
        self._clock = clock
        self._rejected_until = 0
        self._reported = set()
        self._rehearsed = set()
        self.registry = registry or promtext.Registry()
        self._labels = {'host': hostname}
        for result in SEND_RESULTS:
            self._count_send(result, 0)
        for outcome in OUTCOMES:
            self._count_outcome(outcome, 0)

    def step(self):
        """Look once, and send at most one notification."""
        now = self._clock()
        reasons = set()
        snapshot = self._snapshot(now, reasons)
        verdicts = []
        if snapshot is not None:
            reasons |= self._node_reasons(snapshot, now)
            records = self._records(snapshot, reasons)
            if records is not None:
                verdicts = self._judge(snapshot, records, now)
                reasons |= self._outcome_reasons(verdicts, now)
        if now < self._rejected_until:
            reasons.add(NOTIFICATION_REJECTED)

        self._report(verdicts, now)
        self._publish(snapshot, verdicts, reasons, now)
        if reasons:
            LOG.warning('Not notifying from %s: %s', self._hostname,
                        ', '.join(sorted(reasons)))
            return
        if any(verdict['outcome'] == IN_FLIGHT for verdict in verdicts):
            return
        touched = [verdict['touched_at'] for verdict in verdicts
                   if verdict['touched_at'] is not None]
        if touched and now - max(touched) < self._min_gap:
            return
        for verdict in verdicts:
            if verdict['outcome'] == CANDIDATE:
                self._send(verdict['instance'], now)
                return

    def _snapshot(self, now, reasons):
        try:
            snapshot = self._fetch()
        except Exception as exc:
            LOG.warning('Cannot read the exporter state: %s', exc)
            snapshot = None
        if (snapshot is None or
                now - snapshot['taken_at'] > self._max_state_age):
            reasons.add(EXPORTER_UNAVAILABLE)
            return None
        return snapshot

    def _node_reasons(self, snapshot, now):
        reasons = set()
        host = snapshot['host']
        if not host['up']:
            reasons.add(HOST_LXCFS_DOWN)
        elif host['lxcfs_started_at'] is None:
            reasons.add(LXCFS_PROCESS_UNKNOWN)
        # A container started while incusd is stale binds the dead mount
        # again, so restarting one would interrupt it for nothing.
        if not snapshot['incusd']['fresh']:
            reasons.add(INCUSD_VIEW_STALE)
        # A container started now would come up with a label that stops
        # its processes from signalling each other: a restart would trade
        # one fault for another.
        if snapshot['incusd']['launches_stacked']:
            reasons.add(INCUSD_LAUNCHES_STACKED)
        if snapshot['instances'] is None:
            reasons.add(INCUS_API_UNAVAILABLE)
        recent = [change for change in host.get('device_changes', [])
                  if now - change <= self._flap_window]
        if len(recent) >= self._flap_threshold:
            reasons.add(LXCFS_FLAPPING)
        return reasons

    def _records(self, snapshot, reasons):
        """Return the newest notification of this generation per instance.

        Without the records there is no telling what was already asked
        for, so failing to read them blocks sending.
        """
        since = snapshot['host']['lxcfs_started_at']
        if since is None:
            return {}
        try:
            listed = self._masakari.records(since)
        except Exception as exc:
            LOG.warning('Cannot list Masakari notifications: %s', exc)
            reasons.add(MASAKARI_UNAVAILABLE)
            return None
        newest = {}
        for record in listed:
            payload = record['payload']
            if (payload.get('event') != self._event or
                    payload.get('vir_domain_event') != self._detail):
                continue
            uuid = payload.get('instance_uuid')
            if (uuid not in newest or
                    record['generated_at'] > newest[uuid]['generated_at']):
                newest[uuid] = record
        return newest

    def _judge(self, snapshot, records, now):
        verdicts = []
        for instance in snapshot['instances'] or []:
            record = records.get(instance['nova_uuid'])
            outcome = self._outcome(instance, record, now)
            if outcome is None:
                continue
            touched_at = None
            if record is not None:
                touched_at = max(record['generated_at'],
                                 record['updated_at'] or 0)
            verdicts.append({'instance': instance, 'record': record,
                             'outcome': outcome, 'touched_at': touched_at})
        return verdicts

    def _outcome(self, instance, record, now):
        stale = instance['state'] == mountinfo.STALE
        if record is None:
            if stale and instance['confirmed'] and instance['nova_uuid']:
                return CANDIDATE
            return None

        overdue = now - record['generated_at'] > self._recovery_timeout
        status = record['status']
        if status in ('error', 'failed'):
            return FAILED
        if status == 'ignored':
            return IGNORED if stale else None
        if status != 'finished':
            return TIMEOUT if overdue else IN_FLIGHT

        # The engine reports finished both after a recovery and after
        # skipping an instance that did not ask for one. Only a process
        # started after the notification tells the two apart.
        restarted = (instance['started_at'] is not None and
                     instance['started_at'] > record['generated_at'])
        if not restarted:
            return SKIPPED if stale else None
        if instance['state'] == mountinfo.FRESH:
            return RECOVERED
        if stale and instance['confirmed']:
            return INEFFECTIVE
        return TIMEOUT if overdue else IN_FLIGHT

    def _outcome_reasons(self, verdicts, now):
        reasons = set()
        if any(verdict['outcome'] == INEFFECTIVE for verdict in verdicts):
            reasons.add(RECOVERY_INEFFECTIVE)
        settled = sorted(
            (verdict for verdict in verdicts
             if verdict['outcome'] in OUTCOMES),
            key=lambda verdict: verdict['record']['generated_at'])
        latest = settled[-self._error_threshold:]
        if (len(latest) == self._error_threshold and
                all(verdict['outcome'] in FAILURES for verdict in latest) and
                now - latest[-1]['touched_at'] < self._error_cooldown):
            reasons.add(RECOVERY_ERRORS)
        return reasons

    def _send(self, instance, now):
        payload = {'event': self._event,
                   'instance_uuid': instance['nova_uuid'],
                   'vir_domain_event': self._detail}
        if self._dry_run:
            if instance['nova_uuid'] not in self._rehearsed:
                self._rehearsed.add(instance['nova_uuid'])
                LOG.info('Dry run: would notify for %s with %s',
                         instance['name'], payload)
                self._count_send(DRY_RUN)
            return
        result, detail = self._masakari.create(self._hostname, now, payload)
        self._count_send(result)
        if result == ACCEPTED:
            LOG.info('Notified for %s (%s) as %s', instance['name'],
                     instance['nova_uuid'], detail)
            return
        LOG.error('Notification for %s was not accepted (%s): %s',
                  instance['name'], result, detail)
        if result == REJECTED:
            # A rejection is a configuration error, such as the host
            # missing from the segment, and repeating it changes nothing.
            self._rejected_until = now + self._reject_cooldown

    def _report(self, verdicts, now):
        """Count each settled notification once."""
        for verdict in verdicts:
            if verdict['outcome'] not in OUTCOMES:
                continue
            key = (verdict['record']['uuid'], verdict['outcome'])
            if key in self._reported:
                continue
            self._reported.add(key)
            self._count_outcome(verdict['outcome'])
            LOG.info('Notification %s for %s ended as %s', key[0],
                     verdict['instance']['name'], verdict['outcome'])
            if verdict['outcome'] == RECOVERED:
                self.registry.observe(
                    PREFIX + '_recovery_seconds',
                    'Time from the notification to the restarted '
                    'instance.',
                    verdict['instance']['started_at'] -
                    verdict['record']['generated_at'], self._labels)

    def _count_send(self, result, amount=1):
        self.registry.counter(PREFIX + '_notifications_total',
                              'Notifications by the result of sending.',
                              dict(self._labels, result=result), amount)

    def _count_outcome(self, outcome, amount=1):
        self.registry.counter(PREFIX + '_recoveries_total',
                              'Notifications by what became of the '
                              'instance.',
                              dict(self._labels, result=outcome), amount)

    def _publish(self, snapshot, verdicts, reasons, now):
        registry = self.registry
        for reason in BLOCK_REASONS:
            registry.gauge(PREFIX + '_autoheal_blocked',
                           'Whether notifications are withheld, by reason.',
                           reason in reasons,
                           dict(self._labels, reason=reason))

        awaiting = {}
        for verdict in verdicts:
            if (verdict['outcome'] in OUTCOMES and
                    verdict['outcome'] != RECOVERED):
                awaiting[verdict['instance']['name']] = verdict['instance']
        for instance in (snapshot or {}).get('instances') or []:
            # Without a Nova UUID there is nobody to notify.
            if (instance['state'] == mountinfo.STALE and
                    instance['confirmed'] and not instance['nova_uuid']):
                awaiting[instance['name']] = instance
        registry.clear_gauge(PREFIX + '_instance_awaiting_manual')
        for name, instance in awaiting.items():
            registry.gauge(PREFIX + '_instance_awaiting_manual',
                           'Stale instances that will not be recovered '
                           'automatically.', 1,
                           dict(self._labels, incus_instance=name,
                                nova_uuid=instance['nova_uuid'] or ''))

        registry.gauge(PREFIX + '_recovery_in_flight',
                       'Notifications that have not settled yet.',
                       sum(verdict['outcome'] == IN_FLIGHT
                           for verdict in verdicts), self._labels)
        registry.gauge(PREFIX + '_candidates',
                       'Stale instances waiting for a notification.',
                       sum(verdict['outcome'] == CANDIDATE
                           for verdict in verdicts), self._labels)
        registry.gauge(PREFIX + '_dry_run',
                       'Whether notifications are only logged.',
                       self._dry_run, self._labels)
        registry.gauge(PREFIX + '_last_loop_timestamp_seconds',
                       'When the last loop finished.', now, self._labels)

    def metrics(self):
        return promtext.CONTENT_TYPE, self.registry.render()

    def routes(self):
        return {'/metrics': self.metrics}

    def run(self, interval, stop):
        """Loop until the event is set."""
        while not stop.is_set():
            try:
                self.step()
            except Exception:
                LOG.exception('The loop failed')
                self.registry.counter(PREFIX + '_loop_errors_total',
                                      'Errors of the notifier itself.',
                                      self._labels)
            stop.wait(interval)
