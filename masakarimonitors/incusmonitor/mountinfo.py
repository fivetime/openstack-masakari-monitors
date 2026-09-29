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

"""Tell LXCFS mounts apart by the device they sit on.

Every FUSE mount gets its own anonymous device number, so a restarted LXCFS
serves a different device from the one that containers bound before the
restart. A container still holding the old device is looking at a mount
nobody serves any more, and comparing the two numbers is enough to know
that without reading anything through FUSE.
"""

import collections
import re

LXCFS_FSTYPE = 'fuse.lxcfs'

# The container sees what the host serves.
FRESH = 'fresh'
# The container is bound to a device the host no longer serves.
STALE = 'stale'
# The devices agree but the read failed, so the cause is not known.
UNREADABLE = 'unreadable'
# The container has no LXCFS mount at all.
ABSENT = 'absent'
# There was nothing to compare against or the container could not be read.
UNKNOWN = 'unknown'
# The container was deliberately not judged in this sweep.
SKIPPED = 'skipped'

STATES = (FRESH, STALE, UNREADABLE, ABSENT, UNKNOWN, SKIPPED)

Mount = collections.namedtuple(
    'Mount', ['mount_id', 'device', 'mount_point', 'fstype'])

_OCTAL_ESCAPE = re.compile(r'\\([0-7]{3})')


def _unescape(value):
    return _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)),
                             value)


def parse(text):
    """Return the mounts listed in the text of a mountinfo file."""
    mounts = []
    for line in text.splitlines():
        fields = line.split()
        try:
            # Optional fields sit between the mount options and the
            # separator, so the separator is never before the seventh field.
            separator = fields.index('-', 6)
            mount_id = int(fields[0])
            fstype = fields[separator + 1]
        except (ValueError, IndexError):
            continue
        mounts.append(Mount(mount_id=mount_id, device=fields[2],
                            mount_point=_unescape(fields[4]), fstype=fstype))
    return mounts


def lxcfs_devices(mounts):
    """Return every device that an LXCFS mount in the list sits on."""
    return {mount.device for mount in mounts
            if mount.fstype == LXCFS_FSTYPE}


def live_device(mounts, mount_point):
    """Return the device LXCFS serves at the mount point, or None.

    A restart can leave the old mount stacked under the new one. The newest
    mount is the one a lookup resolves to, and it has the highest mount ID.
    """
    candidates = [mount for mount in mounts
                  if mount.fstype == LXCFS_FSTYPE and
                  mount.mount_point == mount_point]
    if not candidates:
        return None
    return max(candidates, key=lambda mount: mount.mount_id).device


def classify(devices, live, readable):
    """Judge one container from its devices and the result of a read.

    :param devices: devices of the container's LXCFS mounts.
    :param live: device the host serves, or None when it serves none.
    :param readable: whether a read through the container's view succeeded.
    """
    if not devices:
        return ABSENT
    if live is None:
        return UNKNOWN
    if set(devices) != {live}:
        return STALE
    return FRESH if readable else UNREADABLE
