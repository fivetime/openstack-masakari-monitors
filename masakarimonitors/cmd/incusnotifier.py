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

"""Starter script for the Incus notifier."""

import functools
import signal
import sys
import threading

from oslo_log import log as logging

import masakarimonitors.conf
from masakarimonitors import config
from masakarimonitors.incusmonitor import httpd
from masakarimonitors.incusmonitor import notifier


CONF = masakarimonitors.conf.CONF
LOG = logging.getLogger(__name__)


def build():
    conf = CONF.incus_notifier
    return notifier.Notifier(
        functools.partial(notifier.fetch_state, conf.state_url),
        notifier.Masakari(), CONF.hostname,
        event=conf.event, detail=conf.event_detail,
        min_gap=conf.min_gap, recovery_timeout=conf.recovery_timeout,
        max_state_age=conf.max_state_age, flap_window=conf.flap_window,
        flap_threshold=conf.flap_threshold,
        error_threshold=conf.error_threshold,
        error_cooldown=conf.error_cooldown,
        reject_cooldown=conf.reject_cooldown, dry_run=conf.dry_run)


def main():
    config.parse_args(sys.argv)
    logging.setup(CONF, "masakarimonitors")

    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *args: stop.set())

    service = build()
    conf = CONF.incus_notifier
    httpd.serve(conf.bind_host, conf.bind_port, service.routes())
    LOG.info('Reading %s every %s seconds as %s%s', conf.state_url,
             conf.loop_interval, CONF.hostname,
             ' (dry run)' if conf.dry_run else '')
    service.run(conf.loop_interval, stop)
