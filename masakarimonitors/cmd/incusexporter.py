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

"""Starter script for the Incus exporter."""

import signal
import sys
import threading

from oslo_log import log as logging

import masakarimonitors.conf
from masakarimonitors import config
from masakarimonitors.incusmonitor import exporter
from masakarimonitors.incusmonitor import httpd
from masakarimonitors.incusmonitor import incus_api
from masakarimonitors.incusmonitor import probe
from masakarimonitors.incusmonitor import procfs


CONF = masakarimonitors.conf.CONF
LOG = logging.getLogger(__name__)


def build():
    conf = CONF.incus_exporter
    prober = probe.Prober(
        incus_api.Client(conf.socket_path, conf.api_timeout),
        procfs.Proc(conf.proc_root, conf.read_timeout),
        lxcfs_path=conf.lxcfs_path, project=conf.project,
        boot_grace=conf.boot_grace)
    return exporter.Exporter(prober, CONF.hostname,
                             confirmations=conf.stale_confirmations)


def main():
    config.parse_args(sys.argv)
    logging.setup(CONF, "masakarimonitors")

    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *args: stop.set())

    service = build()
    conf = CONF.incus_exporter
    httpd.serve(conf.bind_host, conf.bind_port, service.routes())
    LOG.info('Probing every %s seconds, serving on %s:%s',
             conf.probe_interval, conf.bind_host, conf.bind_port)
    service.run(conf.probe_interval, stop)
