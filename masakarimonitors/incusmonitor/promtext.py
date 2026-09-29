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

"""Metrics in the Prometheus text format, without a client library.

The format is a few lines to emit, and the exporter runs privileged on
every compute node, which is a poor place to carry a dependency that is
not needed.
"""

import collections
import threading

CONTENT_TYPE = 'text/plain; version=0.0.4; charset=utf-8'
DEFAULT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)


def _escape(value):
    return (str(value).replace('\\', '\\\\').replace('"', '\\"')
            .replace('\n', '\\n'))


def _labels(pairs):
    if not pairs:
        return ''
    return '{%s}' % ','.join('%s="%s"' % (key, _escape(value))
                             for key, value in sorted(pairs.items()))


def _key(labels):
    return tuple(sorted((labels or {}).items()))


class _Series(object):

    def __init__(self, kind, help_text):
        self.kind = kind
        self.help = help_text
        self.values = {}


class Registry(object):

    def __init__(self):
        self._lock = threading.Lock()
        self._series = collections.OrderedDict()

    def _series_for(self, name, kind, help_text):
        series = self._series.get(name)
        if series is None:
            series = _Series(kind, help_text)
            self._series[name] = series
        return series

    def counter(self, name, help_text, labels=None, amount=1):
        """Add to a counter. An amount of zero makes the series exist."""
        with self._lock:
            series = self._series_for(name, 'counter', help_text)
            key = _key(labels)
            series.values[key] = series.values.get(key, 0) + amount

    def gauge(self, name, help_text, value, labels=None):
        with self._lock:
            series = self._series_for(name, 'gauge', help_text)
            series.values[_key(labels)] = value

    def observe(self, name, help_text, value, labels=None,
                buckets=DEFAULT_BUCKETS):
        with self._lock:
            series = self._series_for(name, 'histogram', help_text)
            entry = series.values.get(_key(labels))
            if entry is None:
                entry = {'buckets': dict.fromkeys(buckets, 0),
                         'count': 0, 'sum': 0.0}
                series.values[_key(labels)] = entry
            entry['count'] += 1
            entry['sum'] += value
            for bound in buckets:
                if value <= bound:
                    entry['buckets'][bound] += 1

    def clear_gauge(self, name):
        """Forget a gauge's series so that vanished labels stop reporting."""
        with self._lock:
            series = self._series.get(name)
            if series is not None and series.kind == 'gauge':
                series.values.clear()

    def render(self):
        lines = []
        with self._lock:
            for name, series in self._series.items():
                lines.append('# HELP %s %s' % (name, series.help))
                lines.append('# TYPE %s %s' % (name, series.kind))
                for key, value in sorted(series.values.items()):
                    labels = dict(key)
                    if series.kind != 'histogram':
                        lines.append('%s%s %s'
                                     % (name, _labels(labels), _number(value)))
                        continue
                    for bound in sorted(value['buckets']):
                        lines.append('%s_bucket%s %d' % (
                            name, _labels(dict(labels, le=_number(bound))),
                            value['buckets'][bound]))
                    lines.append('%s_bucket%s %d' % (
                        name, _labels(dict(labels, le='+Inf')),
                        value['count']))
                    lines.append('%s_sum%s %s' % (name, _labels(labels),
                                                  _number(value['sum'])))
                    lines.append('%s_count%s %d' % (name, _labels(labels),
                                                    value['count']))
        return '\n'.join(lines) + '\n'


def _number(value):
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, int):
        return str(value)
    # repr keeps every digit of a timestamp, which %g would round away.
    return repr(float(value))
