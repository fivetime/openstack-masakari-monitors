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

"""A read-only HTTP server for a handful of fixed paths."""

from http import server
import threading

from oslo_log import log as oslo_logging

LOG = oslo_logging.getLogger(__name__)


def serve(host, port, routes):
    """Serve the routes from a background thread and return the server.

    :param routes: maps a path to a callable returning the content type
        and the body of the response.
    """

    class Handler(server.BaseHTTPRequestHandler):

        def do_GET(self):
            route = routes.get(self.path.split('?', 1)[0])
            if route is None:
                self.send_error(404)
                return
            try:
                content_type, body = route()
            except Exception:
                LOG.exception('Failed to answer %s', self.path)
                self.send_error(500)
                return
            payload = body.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            LOG.debug('%s %s', self.address_string(), format % args)

    httpd = server.ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
