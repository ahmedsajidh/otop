"""Per-route request timings, read from Odoo's own access log.

Odoo logs one line per finished HTTP request through the ``werkzeug`` logger,
and ``odoo.netsvc.PerfFilter`` appends three figures to it::

    2026-07-30 18:36:11,465 2370781 INFO db werkzeug: 1.2.3.4 - - [30/Jul/2026 18:36:11]
      "POST /web/dataset/call_kw/res.partner/web_search_read#res.partner.web_search_read
       HTTP/1.1" 200 - 62 0.034 0.066
                        ^^ ^^^^^ ^^^^^
                        |  |     time spent outside SQL (Python, locks, rendering)
                        |  seconds spent in SQL
                        SQL queries

so the wall time of the request is ``query_time + remaining_time``, already
split into its database and its Python half.  Since Odoo 16 the path of an RPC
call carries a ``#model.method`` fragment, which is what makes this worth
showing at all: ``/web/dataset/call_kw`` on its own says nothing.

Everything here is read-only tailing of a text file -- no instrumentation of
Odoo, no proxy, no extra database work.  What the log cannot tell us is not
guessed:

* requests killed by ``limit_time_real`` / ``limit_time_cpu`` never reach the
  logger, so the very worst offenders can be missing entirely;
* the timings are Odoo-internal and exclude the proxy, the network and the
  browser;
* nothing is shown at all when the instance does not write a log file, or logs
  above INFO level.
"""

from __future__ import annotations

import errno
import os
import re
import time
from collections import deque

#: ``<stamp> <pid> INFO <db> werkzeug: <ip> - - [<date>] "<method> <path> HTTP/x" <code> <size> <perf>``
LINE_RE = re.compile(
    r"^(?P<date>\d{4}-\d\d-\d\d) (?P<clock>\d\d:\d\d:\d\d),\d+ "
    r"(?P<pid>\d+) INFO (?P<db>\S+) werkzeug: "
    r"\S+ - - \[[^\]]*\] "
    r'"(?P<method>[A-Z]+) (?P<path>.*?) HTTP/[\d.]+" '
    r"(?P<status>\d{3}) (?P<size>\S+)"
    r"(?: (?P<queries>\d+|-) (?P<query_time>[\d.]+|-) (?P<remaining>[\d.]+|-))?"
)

#: Path segments that are an identifier rather than a route.
ID_RE = re.compile(r"^\d+$")
HASH_RE = re.compile(r"^[0-9a-f]{6,}$", re.IGNORECASE)
MIXED_RE = re.compile(r"^\d+[-_]\S*$")

CALL_KW_RE = re.compile(r"^/web/dataset/call_kw(?:/(?P<model>[^/]+)/(?P<method>[^/?#]+))?")

SORTS = ("total", "max", "avg", "calls")
SORT_LABELS = {
    "total": "total time",
    "max": "slowest",
    "avg": "average",
    "calls": "calls",
}

DEFAULT_WINDOW = 900.0          # 15 minutes
DEFAULT_MAX_EVENTS = 20000
SEED_BYTES = 1024 * 1024        # how much of an existing log to read at startup
CHUNK_BYTES = 4 * 1024 * 1024   # never read more than this in one poll

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
class _StampCache:
    """``time.strptime`` is far too slow to run on every log line."""

    def __init__(self):
        self._minute = None
        self._base = 0.0

    def to_epoch(self, date, clock):
        minute = date + clock[:6]
        if minute != self._minute:
            try:
                parsed = time.strptime(date + " " + clock[:5], "%Y-%m-%d %H:%M")
                self._base = time.mktime(parsed)
            except (ValueError, OverflowError):
                return None
            self._minute = minute
        try:
            return self._base + int(clock[6:8])
        except ValueError:
            return None


def normalise(method, path):
    """Collapse one request path into a stable route key.

    Identifiers, asset hashes and query strings vary per request and would
    otherwise scatter one route over thousands of buckets.
    """
    path = path.split("?", 1)[0]
    path, _, fragment = path.partition("#")
    if fragment:
        # Odoo already resolved the RPC target for us: model.method.
        return fragment
    call = CALL_KW_RE.match(path)
    if call and call.group("model"):
        return "%s.%s" % (call.group("model"), call.group("method"))

    segments = []
    for segment in path.split("/"):
        if segment and (ID_RE.match(segment) or MIXED_RE.match(segment)
                        or (HASH_RE.match(segment) and any(c.isdigit() for c in segment))):
            segments.append("*")
        else:
            segments.append(segment)
    return "%s %s" % (method, "/".join(segments) or "/")


def parse_line(line, stamps=None):
    """Return one request as a dict, or None for any other log line."""
    match = LINE_RE.match(_ANSI_RE.sub("", line))
    if not match:
        return None
    fields = match.groupdict()

    query_time = _float(fields["query_time"])
    remaining = _float(fields["remaining"])
    if query_time is None or remaining is None:
        total = None                                    # PerfFilter had nothing
    else:
        total = query_time + remaining

    stamps = stamps or _StampCache()
    return {
        "time": stamps.to_epoch(fields["date"], fields["clock"]),
        "pid": int(fields["pid"]),
        "database": fields["db"],
        "method": fields["method"],
        "path": fields["path"],
        "route": normalise(fields["method"], fields["path"]),
        "status": int(fields["status"]),
        "queries": _int(fields["queries"]),
        "query_time": query_time,
        "remaining": remaining,
        "total": total,
    }


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# tailing
# ---------------------------------------------------------------------------
def _cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            return handle.read().decode("utf-8", "replace").split("\0")
    except OSError:
        return []


def _cwd(pid):
    """Working directory of a process, when the kernel lets us look.

    ``/proc/<pid>/cwd`` needs ptrace-level access.  systemd starts Odoo as root
    and drops to ``User=odoo``, which clears the dumpable flag, so the *master*
    is unreadable even to the odoo user itself -- while the workers it forked
    afterwards are readable and inherited the very same directory.
    """
    try:
        return os.readlink("/proc/%d/cwd" % pid)
    except OSError:
        return None


def logfile_argument(argv):
    """The ``--logfile`` value on a command line, possibly relative."""
    path = None
    for index, argument in enumerate(argv):
        if argument.startswith("--logfile="):
            path = argument.split("=", 1)[1]
        elif argument == "--logfile" and index + 1 < len(argv):
            path = argv[index + 1]
    return path or None


def discover_logfile(pid, cwd_pids=(), bases=()):
    """The ``--logfile`` of a running Odoo, made absolute.

    CloudPepper (and most systemd units) pass a *relative* path, so the command
    line alone is not enough; it is resolved against the master's working
    directory, then against any worker's (same directory, readable more often),
    then against the directories given in `bases` -- normally where the
    instance's odoo.conf lives.  Returns None when there is no logfile argument
    at all: that instance logs to stdout or the journal and has no file to tail.
    """
    if not pid:
        return None
    path = logfile_argument(_cmdline(pid))
    if not path:
        return None
    if os.path.isabs(path):
        return path

    candidates = []
    for candidate_pid in (pid,) + tuple(cwd_pids):
        directory = _cwd(candidate_pid)
        if directory:
            candidates.append(os.path.normpath(os.path.join(directory, path)))
    for base in bases:
        if base:
            candidates.append(os.path.normpath(os.path.join(base, path)))

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0] if candidates else None


class LogTailer:
    """Incremental reader for a growing (and rotating) log file.

    Only the bytes appended since the previous poll are read.  Both rotation
    styles are handled: a new inode (``create``) and, as logrotate is set up
    for Odoo, truncation in place (``copytruncate``), which is detected by the
    file shrinking below our offset.
    """

    def __init__(self, path=None, seed_bytes=SEED_BYTES, chunk_bytes=CHUNK_BYTES):
        self.path = path
        self.seed_bytes = seed_bytes
        self.chunk_bytes = chunk_bytes
        self.error = None
        self.bytes_read = 0
        self._offset = 0
        self._inode = None
        self._partial = ""
        self._started = False
        self._drop_first = False

    def reset(self, path):
        if path == self.path:
            return
        self.__init__(path, self.seed_bytes, self.chunk_bytes)

    def read(self):
        """Return the list of new complete lines.  Never raises."""
        if not self.path:
            self.error = "no log file"
            return []
        try:
            status = os.stat(self.path)
        except OSError as exc:
            self.error = ("log file not found" if exc.errno == errno.ENOENT
                          else "cannot stat log file: %s" % exc.strerror)
            self._started = False
            return []

        if self._inode is None or status.st_ino != self._inode:
            self._inode = status.st_ino
            # First sight of a file that already has history: start near its
            # end.  A file that appeared while we were running (rotation with
            # create) is read from the beginning -- it is new, and short.
            if self._started:
                self._offset = 0
            else:
                self._offset = max(0, status.st_size - self.seed_bytes)
                self._drop_first = self._offset > 0
            self._partial = ""
        elif status.st_size < self._offset:              # copytruncate
            self._offset = 0
            self._partial = ""
        self._started = True

        if status.st_size <= self._offset:
            self.error = None
            return []

        try:
            with open(self.path, "rb") as handle:
                handle.seek(self._offset)
                data = handle.read(min(self.chunk_bytes, status.st_size - self._offset))
        except OSError as exc:
            self.error = ("permission denied" if exc.errno == errno.EACCES
                          else "cannot read log file: %s" % exc.strerror)
            return []

        self.error = None
        self._offset += len(data)
        self.bytes_read += len(data)
        text = self._partial + data.decode("utf-8", "replace")
        lines = text.split("\n")
        self._partial = lines.pop()
        if self._partial and len(self._partial) > 1024 * 64:
            self._partial = ""                          # a line this long is not ours
        if lines and self._drop_first:
            # A seeded read starts in the middle of a line: drop that fragment.
            self._drop_first = False
            lines.pop(0)
        return lines


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------
class RouteStats:
    """A rolling window of requests, aggregated per route on demand.

    The window is measured against the newest log timestamp rather than the
    wall clock, so a log written in another timezone -- or one that simply
    stopped -- still yields a coherent picture instead of an empty one.
    """

    def __init__(self, window=DEFAULT_WINDOW, max_events=DEFAULT_MAX_EVENTS):
        self.window = window
        self.max_events = max_events
        self.events = deque()
        self.newest = None
        self.dropped = 0
        self.untimed = 0
        self.seen = 0

    def add(self, request):
        if request is None:
            return
        stamp = request.get("time")
        if stamp is None:
            stamp = self.newest if self.newest is not None else time.time()
        self.seen += 1
        if request.get("total") is None:
            self.untimed += 1
            return
        if self.newest is None or stamp > self.newest:
            self.newest = stamp
        self.events.append((
            stamp,
            request["route"],
            request["total"],
            request.get("query_time") or 0.0,
            request.get("queries") or 0,
            request["status"],
        ))
        self._prune()

    def _prune(self):
        cutoff = (self.newest or 0) - self.window
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()
        while len(self.events) > self.max_events:
            self.events.popleft()
            self.dropped += 1

    def summary(self, sort="total", limit=8):
        """Aggregate the window.  Cheap enough to run on every frame."""
        self._prune()
        buckets = {}
        errors = 0
        total_time = 0.0
        for stamp, route, total, query_time, queries, status in self.events:
            bucket = buckets.get(route)
            if bucket is None:
                bucket = buckets[route] = {
                    "route": route, "calls": 0, "total": 0.0, "sql": 0.0,
                    "queries": 0, "max": 0.0, "errors": 0, "times": [],
                }
            bucket["calls"] += 1
            bucket["total"] += total
            bucket["sql"] += query_time
            bucket["queries"] += queries
            bucket["times"].append(total)
            if total > bucket["max"]:
                bucket["max"] = total
            if status >= 400:
                bucket["errors"] += 1
                errors += 1
            total_time += total

        rows = []
        for bucket in buckets.values():
            times = sorted(bucket["times"])
            calls = bucket["calls"]
            rows.append({
                "route": bucket["route"],
                "calls": calls,
                "total": bucket["total"],
                "avg": bucket["total"] / calls,
                "p95": times[min(len(times) - 1, int(0.95 * len(times)))],
                "max": bucket["max"],
                "sql": bucket["sql"],
                "sql_share": (100.0 * bucket["sql"] / bucket["total"]
                              if bucket["total"] else None),
                "queries": bucket["queries"] / float(calls),
                "errors": bucket["errors"],
            })
        key = {"total": lambda r: r["total"], "max": lambda r: r["max"],
               "avg": lambda r: r["avg"], "calls": lambda r: r["calls"]}
        rows.sort(key=key.get(sort, key["total"]), reverse=True)

        span = 0.0
        if self.events:
            span = max(0.0, self.events[-1][0] - self.events[0][0])
        return {
            "rows": rows[:limit],
            "distinct": len(buckets),
            "requests": len(self.events),
            "total_time": total_time,
            "errors": errors,
            "span": span,
            "window": self.window,
            "rps": (len(self.events) / span) if span >= 1 else None,
            "newest": self.newest,
            "untimed": self.untimed,
            "dropped": self.dropped,
        }


class RouteWatcher:
    """Tailer plus statistics for one instance."""

    def __init__(self, instance, window=DEFAULT_WINDOW, max_events=DEFAULT_MAX_EVENTS):
        self.instance = instance
        self.tailer = LogTailer(instance.logfile)
        self.stats = RouteStats(window, max_events)
        self.stamps = _StampCache()
        self.parsed = 0
        self.lines = 0

    def _bases(self):
        """Directories a relative --logfile may be relative to.

        The instance directory -- where odoo.conf lives, and what process_match
        usually points at -- is the working directory of every Odoo service
        started this way, and unlike /proc/<pid>/cwd it needs no privileges.
        """
        bases = []
        for value in (getattr(self.instance, "odoo_conf", None),
                      getattr(self.instance, "process_match", None)):
            if value and os.path.isabs(value):
                bases.append(value if os.path.isdir(value) else os.path.dirname(value))
        return bases

    def sample(self, sort="total", limit=8, master_pid=None, worker_pids=()):
        """Read whatever is new and return the panel data.  Never raises."""
        if not self.tailer.path and master_pid:
            found = discover_logfile(master_pid, worker_pids, self._bases())
            if found:
                self.instance.logfile = found
                self.tailer.reset(found)

        database = self.instance.database
        try:
            for line in self.tailer.read():
                self.lines += 1
                request = parse_line(line, self.stamps)
                if request is None:
                    continue
                # One log file per instance, but a shared file would mix
                # databases; keep only ours when we know which one that is.
                if database and request["database"] not in (database, "?"):
                    continue
                self.parsed += 1
                self.stats.add(request)
        except Exception as exc:                        # noqa: BLE001
            self.tailer.error = "%s: %s" % (exc.__class__.__name__, exc)

        summary = self.stats.summary(sort, limit)
        summary["path"] = self.tailer.path
        summary["sort"] = sort if sort in SORTS else "total"
        summary["error"] = self.tailer.error
        summary["available"] = bool(self.tailer.path) and not self.tailer.error
        if summary["available"] and not summary["requests"] and not self.parsed:
            summary["note"] = ("no request lines yet -- Odoo logs them at INFO "
                               "level through the werkzeug logger")
        else:
            summary["note"] = None
        return summary


def next_sort(current):
    """The sort mode after `current`, for the 't' key."""
    try:
        return SORTS[(SORTS.index(current) + 1) % len(SORTS)]
    except ValueError:
        return SORTS[0]
