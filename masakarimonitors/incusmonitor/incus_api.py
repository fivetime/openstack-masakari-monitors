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

"""The two Incus queries the probe makes, over the local Unix socket.

Only GET requests are issued. The probe reads the instance list and the
daemon's process ID and changes nothing.
"""

import http.client
import json
import socket
from urllib import parse


class IncusError(Exception):
    pass


class _UnixConnection(http.client.HTTPConnection):

    def __init__(self, socket_path, timeout):
        super(_UnixConnection, self).__init__('incus', timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


class Client(object):

    def __init__(self, socket_path, timeout=10):
        self._socket_path = socket_path
        self._timeout = timeout

    def get(self, path):
        """Return the metadata of a synchronous GET response."""
        connection = _UnixConnection(self._socket_path, self._timeout)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            body = response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise IncusError('GET %s failed: %s' % (path, exc))
        finally:
            connection.close()
        try:
            document = json.loads(body)
        except ValueError:
            raise IncusError('GET %s returned no JSON (HTTP %s)'
                             % (path, response.status))
        if response.status != 200 or document.get('type') == 'error':
            raise IncusError('GET %s failed: HTTP %s %s'
                             % (path, response.status,
                                document.get('error', '')))
        return document.get('metadata')

    def server_pid(self):
        return self.get('/1.0')['environment']['server_pid']

    def instances(self, project):
        return self.get('/1.0/instances?recursion=2&project=%s'
                        % parse.quote(project, safe=''))
