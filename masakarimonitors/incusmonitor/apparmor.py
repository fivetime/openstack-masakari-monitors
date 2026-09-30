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

"""Tell from its AppArmor label whether a container is confined as meant.

Incus confines a container with a profile of its own, whose rules let the
container's processes signal and trace each other by naming that profile as
the peer. A label that stacks ``unconfined`` onto the profile is a different
peer, and the same rules deny what they were written to allow: inside such
a container no process can signal another.

The label is given when the container starts and depends on the label of
the incusd that starts it. Where unprivileged unconfined tasks are
restricted, a profile change made inside a user namespace by a task whose
label is plain ``unconfined`` is stacked instead of replaced, and liblxc
changes profile inside the container's user namespace. An incusd running as
plain ``unconfined`` therefore starts every container with the stacked
label. A label with a name, even one flagged unconfined, is exempt.
"""

import re

# The container runs under its profile and nothing else of this namespace.
CONFINED = 'confined'
# Unconfined is stacked onto the profile.
STACKED = 'stacked'
# The container runs under no profile.
UNCONFINED = 'unconfined'
# The label could not be read.
UNKNOWN = 'unknown'
# The container was deliberately not judged in this sweep.
SKIPPED = 'skipped'

STATES = (CONFINED, STACKED, UNCONFINED, UNKNOWN, SKIPPED)

PLAIN = 'unconfined'
RESTRICTION = ('sys', 'kernel', 'apparmor_restrict_unprivileged_unconfined')

_STACK = '//&'
_MODE = re.compile(r' \([a-z_]+\)$')


def profiles(label):
    """Return the profiles of the viewer's namespace that a label stacks.

    Profiles of a namespace below are what the container loaded for itself
    and say nothing about how the host confines it.
    """
    label = _MODE.sub('', (label or '').strip())
    return [part for part in label.split(_STACK)
            if part and not part.startswith(':')]


def classify(label):
    """Judge one container from the label of its first process."""
    stacked = profiles(label)
    if not stacked:
        return UNKNOWN
    if stacked == [PLAIN]:
        return UNCONFINED
    if PLAIN in stacked:
        return STACKED
    return CONFINED


def launches_stacked(label, restricted):
    """Whether a container started from this label would come up stacked.

    :param label: label of the incusd that would start it.
    :param restricted: whether unprivileged unconfined tasks are restricted.
    """
    return bool(restricted) and profiles(label) == [PLAIN]
