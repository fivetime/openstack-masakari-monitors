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

"""Read what the probe needs from the host's /proc.

Everything goes through the host's process table rather than through the
monitor's own mounts. The monitor's own view of LXCFS goes stale on the very
restart it exists to detect, and the view of process 1 does not.
"""

import os
import threading


class Proc(object):

    def __init__(self, root='/proc', read_timeout=5):
        self.root = root
        self._read_timeout = read_timeout
        self._blocked = {}
        self._ticks = os.sysconf('SC_CLK_TCK')

    def path(self, *parts):
        return os.path.join(self.root, *[str(part) for part in parts])

    def _read(self, *parts):
        with open(self.path(*parts)) as stream:
            return stream.read()

    def mountinfo(self, pid):
        return self._read(pid, 'mountinfo')

    def label(self, pid):
        """Return the AppArmor label of the process."""
        return self._read(pid, 'attr', 'current').strip()

    def flag(self, *parts):
        """Return whether a switch is on, or None where it does not exist."""
        try:
            return self._read(*parts).strip() not in ('', '0')
        except FileNotFoundError:
            return None

    def boot_time(self):
        for line in self._read('stat').splitlines():
            if line.startswith('btime '):
                return int(line.split()[1])
        raise ValueError('no btime in %s' % self.path('stat'))

    def start_time(self, pid):
        """Return when the process started as epoch seconds, or None."""
        try:
            stat = self._read(pid, 'stat')
            # The command name may hold spaces and parentheses, so count
            # the fields from the last closing parenthesis.
            fields = stat[stat.rindex(')') + 2:].split()
            return self.boot_time() + int(fields[19]) / self._ticks
        except (OSError, ValueError, IndexError):
            return None

    def find_by_comm(self, name):
        """Return the IDs of the processes with this command name."""
        found = []
        for entry in os.listdir(self.root):
            if not entry.isdigit():
                continue
            try:
                if self._read(entry, 'comm').strip() == name:
                    found.append(int(entry))
            except OSError:
                continue
        return found

    def readable(self, *parts):
        """Try to read one byte, giving up after the timeout.

        A read from a dead FUSE mount normally fails at once, but one from
        a hung daemon blocks for ever. The read runs in its own thread so
        that a blocked one costs a thread, not the sweep.

        :returns: a tuple of whether the read succeeded and why it did not.
        """
        target = self.path(*parts)
        for key in [key for key, thread in self._blocked.items()
                    if not thread.is_alive()]:
            del self._blocked[key]
        if target in self._blocked:
            return False, 'an earlier read is still blocked'

        result = {}

        def read():
            try:
                with open(target, 'rb') as stream:
                    result['ok'] = bool(stream.read(1))
                if not result['ok']:
                    result['error'] = 'the file is empty'
            except OSError as exc:
                result['error'] = exc.strerror or str(exc)

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        thread.join(self._read_timeout)
        if thread.is_alive():
            self._blocked[target] = thread
            return False, 'timed out after %s seconds' % self._read_timeout
        return result.get('ok', False), result.get('error')
