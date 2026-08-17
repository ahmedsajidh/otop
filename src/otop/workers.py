"""Odoo process discovery and worker status, straight from /proc.

Background (Odoo 15-19, ``odoo/service/server.py``)
---------------------------------------------------
In prefork mode (``workers > 0``) a **master** process binds the HTTP socket and
forks children:

* **HTTP workers** call ``accept()`` on the shared listen socket and then handle
  that one connection synchronously.  The prefork request handler speaks
  HTTP/1.0, so the connection is closed when the response is done and no
  keep-alive socket lingers; websockets are upgraded to HTTP/1.1 but they are
  served by a separate gevent process.  Therefore: *a worker that owns an
  ESTABLISHED socket on the HTTP port is inside a request right now.*
* **Cron workers** never touch the HTTP socket.  ``WorkerCron.start()`` opens a
  connection to the ``postgres`` maintenance database and issues
  ``LISTEN cron_trigger`` on it, and keeps that connection for its whole life.
  No other Odoo process does this.
* The **long polling / websocket** process is spawned as ``odoo-bin gevent``.
* Children call ``setproctitle()``, which only does something when the optional
  ``setproctitle`` package is installed.  With it, command lines become
  ``odoo: WorkerHTTP <pid>`` / ``odoo: WorkerCron <pid> <db>`` and the worker
  type is exact.  Without it every child shares the master's command line.

What that means for busy/idle is documented in README.md; the short version is
that "busy" here means *observed inside a request at the moment of the sample*,
never "used a lot of CPU".
"""

from __future__ import annotations

import os
import re
import time
from collections import deque

PROC = "/proc"
TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

TCP_ESTABLISHED = "01"
TCP_LISTEN = "0A"

# an Odoo cron worker is the only Odoo process connected to these
MAINTENANCE_DATABASES = {"postgres", "template1"}

PROCTITLE_RE = re.compile(r"^odoo:\s+(\w+)\s+(\d+)\s*(.*)$")
ODOO_SCRIPT_RE = re.compile(r"^(odoo-bin|odoo|openerp-server)(\.py)?$")

ROLE_MASTER = "master"
ROLE_HTTP = "http"
ROLE_CRON = "cron"
ROLE_GEVENT = "gevent"
ROLE_UNKNOWN = "unknown"

BUSY = "busy"
IDLE = "idle"
UNKNOWN = "unknown"
NOT_APPLICABLE = "n/a"


# ---------------------------------------------------------------------------
# /proc primitives
# ---------------------------------------------------------------------------
def _read(path):
    try:
        with open(path) as handle:
            return handle.read()
    except (OSError, ValueError):
        return None


def boot_time():
    text = _read(PROC + "/stat") or ""
    for line in text.splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                break
    return None


def read_stat(pid):
    """(ppid, state, threads, cpu_ticks, start_ticks) for a pid, or None."""
    raw = _read("%s/%d/stat" % (PROC, pid))
    if not raw:
        return None
    try:
        rest = raw[raw.rindex(")") + 2:].split()
        return {
            "state": rest[0],
            "ppid": int(rest[1]),
            "cpu_ticks": int(rest[11]) + int(rest[12]),
            "threads": int(rest[17]),
            "start_ticks": int(rest[19]),
        }
    except (ValueError, IndexError):
        return None


def read_rss(pid):
    raw = _read("%s/%d/statm" % (PROC, pid))
    if not raw:
        return None
    try:
        return int(raw.split()[1]) * PAGE_SIZE
    except (ValueError, IndexError):
        return None


def read_cmdline(pid):
    raw = _read("%s/%d/cmdline" % (PROC, pid))
    if raw is None:
        return None
    return raw.replace("\0", " ").strip()


def socket_inodes(pid):
    """Socket inodes held by pid, or None when /proc/<pid>/fd is not readable.

    None means "no permission to look" and must never be treated as "no
    connections" -- that distinction is what keeps busy/idle honest.
    """
    directory = "%s/%d/fd" % (PROC, pid)
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    inodes = set()
    for entry in entries:
        try:
            target = os.readlink(os.path.join(directory, entry))
        except OSError:
            continue
        if target.startswith("socket:["):
            try:
                inodes.add(int(target[8:-1]))
            except ValueError:
                pass
    return inodes


def child_pids(pid):
    """Direct children reported by the kernel, or None if not available."""
    raw = _read("%s/%d/task/%d/children" % (PROC, pid, pid))
    if raw is None:
        return None
    try:
        return {int(value) for value in raw.split()}
    except ValueError:
        return None


class Sockets:
    """One read of /proc/net/tcp[6] per sample.

    ``by_inode``  inode -> (local_port, remote_port, state)
    ``backlog``   listening port -> connections accepted by the kernel but not
                  yet picked up by any worker (sk_ack_backlog).  This is the
                  request queue depth, and it is exact.
    """

    def __init__(self):
        self.by_inode = {}
        self.backlog = {}

    @classmethod
    def read(cls, paths=("/proc/net/tcp", "/proc/net/tcp6")):
        sockets = cls()
        for path in paths:
            text = _read(path)
            if not text:
                continue
            for line in text.splitlines()[1:]:
                fields = line.split()
                if len(fields) < 10:
                    continue
                try:
                    local_port = int(fields[1].rsplit(":", 1)[1], 16)
                    remote_port = int(fields[2].rsplit(":", 1)[1], 16)
                    state = fields[3]
                    queued = int(fields[4].split(":")[1], 16)
                    inode = int(fields[9])
                except (ValueError, IndexError):
                    continue
                if state == TCP_LISTEN:
                    previous = sockets.backlog.get(local_port, 0)
                    sockets.backlog[local_port] = max(previous, queued)
                elif inode:
                    sockets.by_inode[inode] = (local_port, remote_port, state)
        return sockets


# ---------------------------------------------------------------------------
# process table
# ---------------------------------------------------------------------------
PYTHON_RE = re.compile(r"^python[0-9.]*$")


def looks_like_odoo(cmdline):
    """True only when Odoo is the program being executed.

    Matching anywhere in the command line would also match ``grep odoo-bin`` or
    an editor with odoo.conf open, so only the executable position counts:
    ``odoo-bin ...`` or ``python3 /opt/odoo/odoo-bin ...``.
    """
    if not cmdline:
        return False
    if cmdline.startswith("odoo: "):                    # setproctitle
        return True
    tokens = cmdline.split()
    if not tokens:
        return False
    if ODOO_SCRIPT_RE.match(tokens[0].rsplit("/", 1)[-1]):
        return True
    if PYTHON_RE.match(tokens[0].rsplit("/", 1)[-1]) and len(tokens) > 1:
        return bool(ODOO_SCRIPT_RE.match(tokens[1].rsplit("/", 1)[-1]))
    return False


def classify(cmdline):
    """(role, detail, exact) for one command line."""
    match = PROCTITLE_RE.match(cmdline or "")
    if match:
        worker_class, _pid, detail = match.groups()
        if worker_class == "WorkerHTTP":
            return ROLE_HTTP, "", True
        if worker_class == "WorkerCron":
            return ROLE_CRON, detail.strip(), True
        return ROLE_UNKNOWN, worker_class, True
    if cmdline and "gevent" in cmdline.split():
        return ROLE_GEVENT, "", True
    return ROLE_UNKNOWN, "", False


def scan_processes():
    """pid -> {pid, ppid, cmdline} for every visible process."""
    table = {}
    try:
        entries = os.listdir(PROC)
    except OSError:                                     # pragma: no cover
        return table
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        cmdline = read_cmdline(pid)
        if cmdline is None:
            continue
        stat = read_stat(pid)
        if stat is None:
            continue
        table[pid] = {"pid": pid, "ppid": stat["ppid"], "cmdline": cmdline}
    return table


# ---------------------------------------------------------------------------
# sampler
# ---------------------------------------------------------------------------
class WorkerSampler:
    """Stateful sampler: CPU deltas, busy history, cached process table."""

    def __init__(self, busy_cpu_threshold=20.0, window=30, discovery_refresh=10.0):
        self.busy_cpu_threshold = busy_cpu_threshold
        self.window = window
        self.discovery_refresh = discovery_refresh
        self._cpu = {}                  # pid -> (cpu_ticks, timestamp)
        self._history = {}              # pid -> deque of 0/1
        self._table = {}
        self._table_time = 0.0
        self._masters = set()
        self.boot = boot_time()

    # -- process table --------------------------------------------------
    def _refresh_table(self, now):
        """Rebuild the table only when it is stale or the children changed.

        Walking every /proc/<pid>/cmdline is the most expensive thing otop does,
        and pid/ppid/cmdline never change during a process's life, so this only
        happens every `discovery_refresh` seconds -- or immediately when the
        kernel says a known master has gained or lost a child.
        """
        stale = not self._table or (now - self._table_time) >= self.discovery_refresh
        if not stale:
            for master in self._masters:
                actual = child_pids(master)
                if actual is None:
                    continue
                cached = {p for p, info in self._table.items() if info["ppid"] == master}
                if cached != actual:
                    stale = True
                    break
        if stale:
            self._table = scan_processes()
            self._table_time = now
        return self._table

    # -- per process ----------------------------------------------------
    def _cpu_percent(self, pid, cpu_ticks, now):
        previous = self._cpu.get(pid)
        self._cpu[pid] = (cpu_ticks, now)
        if not previous:
            return None
        old_ticks, old_time = previous
        elapsed = now - old_time
        if elapsed <= 0 or cpu_ticks < old_ticks:
            return None
        return round(100.0 * (cpu_ticks - old_ticks) / TICKS / elapsed, 1)

    def _process(self, pid, cmdline, role, detail, exact, instance, sockets,
                 backends_by_port, now):
        stat = read_stat(pid)
        if stat is None:
            return None
        proc = {
            "pid": pid,
            "role": role,
            "detail": detail,
            "role_exact": exact,
            "status": UNKNOWN,
            "status_exact": True,
            "evidence": "none",
            "cpu": self._cpu_percent(pid, stat["cpu_ticks"], now),
            "rss": read_rss(pid),
            "threads": stat["threads"],
            "proc_state": stat["state"],
            "uptime": None,
            "http_conns": None,
            "db_state": None,
            "db_query_age": None,
            "busy_ratio": None,
            "cmdline": cmdline,
        }
        if self.boot:
            proc["uptime"] = max(0.0, now - (self.boot + stat["start_ticks"] / TICKS))

        inodes = socket_inodes(pid)
        backends = []
        if inodes is not None:
            http_conns = 0
            for inode in inodes:
                connection = sockets.by_inode.get(inode)
                if not connection:
                    continue
                local_port, remote_port, state = connection
                if state != TCP_ESTABLISHED:
                    continue
                if instance.http_port and local_port == instance.http_port:
                    http_conns += 1
                elif remote_port == instance.db_port:
                    backend = backends_by_port.get(local_port)
                    if backend:
                        backends.append(backend)
            proc["http_conns"] = http_conns

        active = [b for b in backends if b.get("state") == "active"]
        if backends:
            proc["db_state"] = active[0]["state"] if active else backends[0].get("state")
            proc["db_databases"] = sorted({b.get("datname") for b in backends
                                           if b.get("datname")})
            if active and active[0].get("query_age") is not None:
                proc["db_query_age"] = round(active[0]["query_age"], 1)
        else:
            proc["db_databases"] = []

        self._decide_status(proc, active)
        return proc

    def _decide_status(self, proc, active_backends):
        """Busy/idle, strongest evidence first.  Never CPU alone if avoidable."""
        if proc["role"] == ROLE_MASTER:
            proc["status"] = NOT_APPLICABLE
            proc["evidence"] = "n/a"
            return
        if proc["role"] == ROLE_HTTP and proc["http_conns"] is not None:
            proc["status"] = BUSY if proc["http_conns"] else IDLE
            proc["evidence"] = "socket"
            return
        if active_backends:
            proc["status"] = BUSY
            proc["evidence"] = "database"
            return
        if proc["http_conns"] is not None and proc["role"] in (ROLE_CRON, ROLE_GEVENT):
            # sockets readable, nothing running in the database either
            proc["status"] = IDLE
            proc["evidence"] = "database" if proc["db_state"] else "socket"
            return
        if proc["cpu"] is not None:
            proc["status"] = BUSY if proc["cpu"] >= self.busy_cpu_threshold else IDLE
            proc["evidence"] = "cpu"
            proc["status_exact"] = False
            return
        proc["status"] = UNKNOWN
        proc["evidence"] = "none"

    # -- typing without setproctitle ------------------------------------
    def _infer_roles(self, rows, instance, notes):
        """rows: list of [pid, cmdline, role, detail, exact]."""
        unknown = [row for row in rows if row[2] == ROLE_UNKNOWN]
        if not unknown:
            return
        for row in unknown:
            for backend in row[5]:
                if backend.get("datname") in MAINTENANCE_DATABASES:
                    row[2], row[4] = ROLE_CRON, True
                    break

        unknown = [row for row in rows if row[2] == ROLE_UNKNOWN]
        if not unknown:
            return
        if instance.workers is None or instance.max_cron_threads is None:
            notes.append("worker types unknown: no odoo_conf, so the expected "
                         "worker counts are not known")
            return
        http_left = max(0, instance.workers - sum(1 for r in rows if r[2] == ROLE_HTTP))
        cron_left = max(0, instance.max_cron_threads
                        - sum(1 for r in rows if r[2] == ROLE_CRON))
        if http_left + cron_left != len(unknown):
            notes.append("child processes (%d) do not match workers=%d + "
                         "max_cron_threads=%d; some types unknown"
                         % (len(rows), instance.workers, instance.max_cron_threads))
            return
        # process_spawn() starts HTTP workers first, then gevent, then cron
        for row in unknown:
            if http_left:
                row[2], http_left = ROLE_HTTP, http_left - 1
            else:
                row[2], cron_left = ROLE_CRON, cron_left - 1
            row[4] = False              # inferred, not confirmed

    # -- public ---------------------------------------------------------
    def sample(self, instance, sockets, backends_by_port, now=None):
        """Return the worker picture for one instance.  Never raises."""
        now = now or time.time()
        result = {
            "running": False,
            "mode": UNKNOWN,
            "error": None,
            "notes": [],
            "master": None,
            "processes": [],
            "http_total": 0,
            "busy": 0,
            "idle": 0,
            "unknown": 0,
            "utilisation": None,
            "utilisation_avg": None,
            "window_seconds": 0,
            "queued": None,
            "in_flight": None,
            "evidence": "none",
        }
        if not instance.process_match:
            result["error"] = "no process_match configured"
            return result

        table = self._refresh_table(now)
        candidates = [info for info in table.values()
                      if instance.process_match in info["cmdline"]
                      and looks_like_odoo(info["cmdline"])]
        if not candidates:
            result["error"] = "not running"
            return result

        candidate_pids = {info["pid"] for info in candidates}
        masters = [info for info in candidates if info["ppid"] not in candidate_pids]
        if not masters:
            masters = [min(candidates, key=lambda info: info["pid"])]
        master = min(masters, key=lambda info: info["pid"])
        if len(masters) > 1:
            result["notes"].append(
                "%d process trees match this instance; showing pid %d"
                % (len(masters), master["pid"]))
        if not os.path.isdir("%s/%d" % (PROC, master["pid"])):
            self._table_time = 0.0                     # force a rescan next sample
            result["error"] = "not running"
            return result

        self._masters = {master["pid"]}
        result["running"] = True
        result["queued"] = sockets.backlog.get(instance.http_port)

        master_proc = self._process(master["pid"], master["cmdline"], ROLE_MASTER,
                                    "", True, instance, sockets, backends_by_port, now)
        result["master"] = master_proc

        children = [info for info in table.values()
                    if info["ppid"] == master["pid"]
                    and os.path.isdir("%s/%d" % (PROC, info["pid"]))]
        if not children:
            result["mode"] = "threaded"
            result["in_flight"] = master_proc["http_conns"] if master_proc else None
            result["notes"].append(
                "threaded mode (workers = 0): no worker processes exist. "
                "'in flight' counts established connections on port %s; "
                "per-request threads cannot be told apart from the process level."
                % instance.http_port)
            return result

        result["mode"] = "prefork"
        children.sort(key=lambda info: info["pid"])

        rows = []
        for info in children:
            role, detail, exact = classify(info["cmdline"])
            backends = self._backends_of(info["pid"], sockets, backends_by_port,
                                         instance)
            rows.append([info["pid"], info["cmdline"], role, detail, exact, backends])
        self._infer_roles(rows, instance, result["notes"])

        for pid, cmdline, role, detail, exact, _backends in rows:
            proc = self._process(pid, cmdline, role, detail, exact, instance,
                                 sockets, backends_by_port, now)
            if proc:
                result["processes"].append(proc)

        self._update_history(result)
        self._summarise(result)
        return result

    def _backends_of(self, pid, sockets, backends_by_port, instance):
        inodes = socket_inodes(pid)
        if not inodes or not backends_by_port:
            return []
        found = []
        for inode in inodes:
            connection = sockets.by_inode.get(inode)
            if not connection:
                continue
            local_port, remote_port, state = connection
            if state == TCP_ESTABLISHED and remote_port == instance.db_port:
                backend = backends_by_port.get(local_port)
                if backend:
                    found.append(backend)
        return found

    def _update_history(self, result):
        live = set()
        for proc in result["processes"]:
            live.add(proc["pid"])
            if proc["status"] not in (BUSY, IDLE):
                continue
            history = self._history.setdefault(proc["pid"], deque(maxlen=self.window))
            history.append(1 if proc["status"] == BUSY else 0)
            proc["busy_ratio"] = round(100.0 * sum(history) / len(history), 1)
            proc["window_samples"] = len(history)
        for pid in list(self._history):
            if pid not in live:
                del self._history[pid]
        for pid in list(self._cpu):
            if pid not in live and pid != (result["master"] or {}).get("pid"):
                del self._cpu[pid]

    def _summarise(self, result):
        http = [p for p in result["processes"] if p["role"] == ROLE_HTTP]
        result["http_total"] = len(http)
        result["busy"] = sum(1 for p in http if p["status"] == BUSY)
        result["idle"] = sum(1 for p in http if p["status"] == IDLE)
        result["unknown"] = sum(1 for p in http if p["status"] == UNKNOWN)
        result["in_flight"] = sum(p["http_conns"] or 0 for p in http
                                  if p["http_conns"] is not None) or 0
        known = result["busy"] + result["idle"]
        if known:
            result["utilisation"] = round(100.0 * result["busy"] / known, 1)
        ratios = [p["busy_ratio"] for p in http if p["busy_ratio"] is not None]
        if ratios:
            result["utilisation_avg"] = round(sum(ratios) / len(ratios), 1)
            result["window_seconds"] = max(p.get("window_samples", 0) for p in http)

        evidences = {p["evidence"] for p in http}
        if "socket" in evidences:
            result["evidence"] = "socket"
        elif evidences & {"database", "cpu"}:
            result["evidence"] = "cpu"
        if result["evidence"] != "socket" and http:
            result["notes"].append(
                "cannot read /proc/<pid>/fd of the workers: busy/idle is a CPU "
                "approximation (marked ~). Run otop as root or as the Odoo user.")
        if any(not p["role_exact"] for p in result["processes"]):
            result["notes"].append(
                "worker types marked ~ are inferred; install the 'setproctitle' "
                "package in the Odoo virtualenv for exact typing")
