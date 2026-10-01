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

from oslo_config import cfg


exporter_opts = [
    cfg.StrOpt('socket_path',
               default='/var/lib/incus/unix.socket',
               help='Unix socket of the local Incus daemon.'),
    cfg.StrOpt('project',
               default='default',
               help='Incus project that holds the Nova instances.'),
    cfg.StrOpt('lxcfs_path',
               default='/var/lib/lxcfs',
               help='Where LXCFS is mounted on the host and in incusd.'),
    cfg.StrOpt('proc_root',
               default='/proc',
               help='The /proc of the host PID namespace.'),
    cfg.IntOpt('probe_interval',
               default=15,
               min=1,
               help='Seconds between two sweeps.'),
    cfg.IntOpt('stale_confirmations',
               default=3,
               min=1,
               help='Consecutive stale sweeps before an instance counts '
                    'as confirmed stale.'),
    cfg.IntOpt('boot_grace',
               default=60,
               min=0,
               help='Seconds after its start during which an instance is '
                    'not judged.'),
    cfg.IntOpt('read_timeout',
               default=5,
               min=1,
               help='Seconds to wait for a read through LXCFS.'),
    cfg.IntOpt('api_timeout',
               default=10,
               min=1,
               help='Seconds to wait for an answer from Incus.'),
    cfg.HostAddressOpt('bind_host',
                       default='0.0.0.0',
                       help='Address the exporter listens on.'),
    cfg.PortOpt('bind_port',
                default=9476,
                help='Port the exporter listens on.'),
]

notifier_opts = [
    cfg.StrOpt('state_url',
               default='http://127.0.0.1:9476/state',
               help='Where the exporter of the same node serves its state.'),
    cfg.IntOpt('loop_interval',
               default=15,
               min=1,
               help='Seconds between two loops.'),
    cfg.StrOpt('event',
               default='INCUS_GUEST_ERROR',
               help='Event name of the notification. The engine recovers '
                    'an instance only for an event it knows.'),
    cfg.StrOpt('event_detail',
               default='LXCFS_STALE',
               help='Detail of the event, sent as vir_domain_event.'),
    cfg.IntOpt('min_gap',
               default=30,
               min=0,
               help='Seconds between a settled notification and the '
                    'next one.'),
    cfg.IntOpt('recovery_timeout',
               default=300,
               min=1,
               help='Seconds after which an unsettled notification counts '
                    'as failed.'),
    cfg.IntOpt('max_state_age',
               default=60,
               min=1,
               help='Seconds after which the exporter state is too old to '
                    'act on.'),
    cfg.IntOpt('flap_window',
               default=600,
               min=1,
               help='Seconds over which LXCFS restarts are counted.'),
    cfg.IntOpt('flap_threshold',
               default=2,
               min=1,
               help='LXCFS restarts within the window that stop '
                    'notifications.'),
    cfg.IntOpt('error_threshold',
               default=2,
               min=1,
               help='Consecutive failed recoveries that stop '
                    'notifications.'),
    cfg.IntOpt('error_cooldown',
               default=3600,
               min=0,
               help='Seconds that failed recoveries stop notifications.'),
    cfg.IntOpt('reject_cooldown',
               default=600,
               min=0,
               help='Seconds that a rejected notification stops further '
                    'ones.'),
    cfg.BoolOpt('dry_run',
                default=False,
                help='Log the notifications instead of sending them.'),
    cfg.ListOpt('armed_hosts',
                default=[],
                help='Hosts whose notifier sends notifications. On any '
                     'other host it runs as a dry run, so that recovery '
                     'can be enabled one node at a time from a setting '
                     'shared by all of them. Empty means every host.'),
    cfg.HostAddressOpt('bind_host',
                       default='0.0.0.0',
                       help='Address the notifier serves its metrics on.'),
    cfg.PortOpt('bind_port',
                default=9477,
                help='Port the notifier serves its metrics on.'),
]


def register_opts(conf):
    conf.register_opts(exporter_opts, group='incus_exporter')
    conf.register_opts(notifier_opts, group='incus_notifier')


def list_opts():
    return {
        'incus_exporter': exporter_opts,
        'incus_notifier': notifier_opts,
    }
