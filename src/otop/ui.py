"""Terminal UI.

The layout is built as plain data -- a list of lines, each line a list of
``(text, style)`` segments -- and only then painted with curses.  That keeps the
drawing code trivial, lets ``otop --once`` print the very same layout without a
terminal, and makes the layout testable without curses at all.
"""

from __future__ import annotations

import time

from .format import (
    bar,
    fit,
    human_bytes,
    human_count,
    human_duration,
    human_percent,
    human_rate,
    human_seconds,
    level,
)
from .routes import SORT_LABELS

INTENSITY = " .:-=+*#%@"

STYLES = ("normal", "title", "label", "dim", "good", "warn", "crit", "busy",
          "idle", "tab", "tab_active", "err", "key")

MIN_WIDTH = 40
MIN_HEIGHT = 10


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _line(*segments):
    return [segment for segment in segments if segment is not None]


def _pair(label, value, style="normal", label_style="label"):
    return [(label + " ", label_style), (value, style)]


def _join(*groups):
    out = []
    for index, group in enumerate(groups):
        if not group:
            continue
        if index and out:
            out.append(("   ", "normal"))
        out.extend(group)
    return out


def _text_width(line):
    return sum(len(text) for text, _style in line)


def _truncate(line, width):
    out = []
    used = 0
    for text, style in line:
        if used >= width:
            break
        room = width - used
        if len(text) > room:
            out.append((fit(text, room), style))
            used = width
        else:
            out.append((text, style))
            used += len(text)
    return out


# ---------------------------------------------------------------------------
# blocks
# ---------------------------------------------------------------------------
def header(snapshot, config, active, width, version):
    host = (snapshot.get("system") or {}).get("host") or {}
    left = [("OTOP", "title"), (" " + version + "  ", "dim"),
            (host.get("hostname") or "", "normal")]
    uptime = host.get("uptime")
    if uptime:
        left.append((" up " + human_seconds(uptime), "dim"))

    tabs = []
    for instance in config.instances:
        style = "tab_active" if instance.key == active else "tab"
        state = snapshot.get("instances", {}).get(instance.key) or {}
        marker = ""
        workers = state.get("workers") or {}
        if workers.get("error") == "not running":
            marker = "!"
        tabs.append((" " + instance.name + marker + " ", style))
        tabs.append((" ", "normal"))

    clock = time.strftime("%H:%M:%S")
    line = left + [("  ", "normal")] + tabs
    padding = width - _text_width(line) - len(clock)
    if padding > 0:
        line.append((" " * padding, "normal"))
    line.append((clock, "dim"))
    return [_truncate(line, width)]


def system_block(snapshot, width):
    system = snapshot.get("system") or {}
    cpu = system.get("cpu") or {}
    memory = system.get("memory") or {}
    lines = []

    gauge = 20 if width >= 80 else 12
    percent = cpu.get("percent")
    line = [("CPU  ", "label"), (bar(percent, gauge), level(percent)),
            (" " + human_percent(percent).rjust(4), level(percent))]
    cores = cpu.get("per_core") or []
    if cores:
        line.append(("  cores ", "dim"))
        line.append((str(len(cores)), "dim"))
        line.append((" ", "normal"))
        for value in cores:
            index = min(len(INTENSITY) - 1, int(value / 100.0 * (len(INTENSITY) - 1)))
            line.append((INTENSITY[index], level(value)))
    load = cpu.get("load")
    if load:
        line.append(("  LOAD ", "label"))
        style = level(100.0 * load[0] / max(1, cpu.get("cores") or 1), 80, 120)
        line.append(("%.2f %.2f %.2f" % tuple(load), style))
    lines.append(_truncate(line, width))

    percent = memory.get("percent")
    line = [("RAM  ", "label"), (bar(percent, gauge), level(percent)),
            (" " + human_percent(percent).rjust(4), level(percent)),
            ("  " + human_bytes(memory.get("used")) + " / "
             + human_bytes(memory.get("total")), "normal"),
            ("  free " + human_bytes(memory.get("available")), "dim")]
    lines.append(_truncate(line, width))

    percent = memory.get("swap_percent")
    if memory.get("swap_total"):
        line = [("SWAP ", "label"), (bar(percent, gauge), level(percent, 25, 60)),
                (" " + human_percent(percent).rjust(4), level(percent, 25, 60)),
                ("  " + human_bytes(memory.get("swap_used")) + " / "
                 + human_bytes(memory.get("swap_total")), "normal")]
    else:
        line = [("SWAP ", "label"), ("disabled", "dim")]
    lines.append(_truncate(line, width))
    return lines


def workers_block(state, width, max_rows):
    workers = (state or {}).get("workers") or {}
    lines = [_truncate([("ODOO WORKERS", "title"),
                        ("  " + (workers.get("mode") or "unknown"), "dim")], width)]

    if workers.get("error"):
        style = "err" if workers["error"] != "not running" else "warn"
        lines.append(_truncate([("  " + workers["error"], style)], width))
        return lines

    if workers.get("mode") == "threaded":
        master = workers.get("master") or {}
        lines.append(_truncate(_join(
            _pair("IN FLIGHT", human_count(workers.get("in_flight")),
                  "busy" if workers.get("in_flight") else "idle"),
            _pair("QUEUED", human_count(workers.get("queued")),
                  "crit" if workers.get("queued") else "dim"),
            _pair("THREADS", human_count(master.get("threads")), "normal"),
            _pair("RSS", human_bytes(master.get("rss")), "normal"),
            _pair("PID", human_count(master.get("pid")), "dim"),
        ), width))
    else:
        util = workers.get("utilisation")
        avg = workers.get("utilisation_avg")
        window = workers.get("window_seconds") or 0
        counters = _join(
            _pair("TOTAL", human_count(workers.get("http_total")), "normal"),
            _pair("BUSY", human_count(workers.get("busy")), "busy"),
            _pair("IDLE", human_count(workers.get("idle")), "idle"),
            _pair("UTIL", human_percent(util), level(util)),
            _pair("AVG", human_percent(avg), level(avg)),
            _pair("QUEUED", human_count(workers.get("queued")),
                  "crit" if workers.get("queued") else "dim"),
        )
        if window:
            counters.append(("   (avg of last %d samples)" % window, "dim"))
        lines.append(_truncate(counters, width))

    processes = list(workers.get("processes") or [])
    if workers.get("master"):
        processes.insert(0, workers["master"])
    if not processes:
        return lines

    wide = width >= 74
    head = "%-7s %-8s %5s %8s %9s %-9s %6s" % (
        "PID", "ROLE", "CPU", "RAM", "UPTIME", "STATUS", "BUSY%")
    if wide:
        head += "  DB"
    lines.append([(fit(head, width), "label")])

    for process in processes[:max_rows]:
        role = process["role"] + ("" if process.get("role_exact", True) else "~")
        if process.get("detail"):
            role = role[:4] + ":" + str(process["detail"])[:3]
        status = process.get("status")
        if status == "busy":
            marker, style = "* BUSY", "busy"
        elif status == "idle":
            marker, style = "o IDLE", "idle"
        elif status == "n/a":
            marker, style = "-", "dim"
        else:
            marker, style = "? UNKN", "dim"
        if not process.get("status_exact", True) and status in ("busy", "idle"):
            marker = "~" + marker
        cpu = process.get("cpu")
        row = [("%-7d %-8s %5s %8s %9s " % (
            process["pid"], fit(role, 8),
            human_percent(cpu, na="-"), human_bytes(process.get("rss"), na="-"),
            human_seconds(process.get("uptime"), na="-")), "normal"),
            ("%-9s" % marker, style),
            ("%6s" % (human_percent(process.get("busy_ratio"), na="-")), "normal")]
        if wide:
            database = process.get("db_state") or "-"
            if process.get("db_query_age") is not None:
                database += " " + human_seconds(process["db_query_age"])
            row.append(("  " + database, "dim"))
        lines.append(_truncate(row, width))

    hidden = len(processes) - max_rows
    if hidden > 0:
        lines.append(_truncate([("  ... %d more process(es)" % hidden, "dim")],
                               width))
    return lines


def storage_block(snapshot, width, max_rows=6):
    storage = snapshot.get("storage") or {}
    disk = snapshot.get("disk") or {}
    lines = [_truncate([("STORAGE", "title")], width)]

    if disk.get("error"):
        lines.append(_truncate(
            [("  %s: %s" % (disk.get("path"), disk["error"]), "err")], width))
        return lines

    percent = disk.get("percent")
    gauge = 20 if width >= 80 else 12
    lines.append(_truncate([
        ("%-6s" % (disk.get("path") or "/"), "label"),
        (bar(percent, gauge), level(percent)),
        (" " + human_percent(percent).rjust(4), level(percent)),
        ("  " + human_bytes(disk.get("used")) + " / " + human_bytes(disk.get("total")),
         "normal"),
        ("   free " + human_bytes(disk.get("free")), "dim"),
    ], width))

    total = disk.get("total") or 0
    rows = list(storage.get("rows") or [])
    rows.append({"label": "Other files", "bytes": storage.get("other"),
                 "note": "disk used minus the rows above", "same_fs": True})
    for row in rows[:max_rows]:
        share = ""
        if total and row.get("bytes"):
            share = "%5.1f%%" % (100.0 * row["bytes"] / total)
        note = row.get("error") or ""
        if not note and row.get("same_fs") is False:
            note = "on another filesystem"
        if not note and row.get("scanning"):
            note = "scanning..."
        if not note and row.get("age"):
            note = "%s ago" % human_seconds(row["age"])
        if not note:
            note = row.get("note") or ""
        lines.append(_truncate([
            ("  %-22s" % fit(row.get("label") or "", 22), "normal"),
            ("%10s " % human_bytes(row.get("bytes")), "normal"),
            ("%6s  " % share, "dim"),
            (note, "dim" if not row.get("error") else "warn"),
        ], width))
    return lines


def postgres_block(state, width, max_queries=3, max_tables=3):
    postgres = (state or {}).get("postgres") or {}
    sizes = (state or {}).get("sizes") or {}
    title = [("POSTGRESQL", "title")]
    if (state or {}).get("database"):
        title.append(("  " + state["database"], "dim"))
    if sizes.get("database"):
        title.append(("  " + human_bytes(sizes["database"]), "normal"))
    lines = [_truncate(title, width)]

    if not postgres.get("available"):
        lines.append(_truncate(
            [("  " + (postgres.get("error") or "unavailable"), "err")], width))
        return lines

    waiting = postgres.get("waiting")
    idle_tx = postgres.get("idle_in_transaction")
    lines.append(_truncate(_join(
        _pair("CONN", human_count(postgres.get("connections")), "normal"),
        _pair("ACTIVE", human_count(postgres.get("active")), "busy"),
        _pair("IDLE", human_count(postgres.get("idle")), "idle"),
        _pair("IDLE-TX", human_count(idle_tx), "warn" if idle_tx else "dim"),
        _pair("WAITING", human_count(waiting), "crit" if waiting else "dim"),
        _pair("LONGEST", human_seconds(postgres.get("longest")), "normal"),
    ), width))

    if postgres.get("restricted"):
        lines.append(_truncate(
            [("  some backends are owned by another role; PostgreSQL hides "
              "their state (GRANT pg_monitor)", "warn")], width))

    for query in (postgres.get("long_queries") or [])[:max_queries]:
        text = query.get("query") or "(query text hidden)"
        lines.append(_truncate([
            ("  %-8s" % ("pid %d" % query["pid"]), "dim"),
            ("%8s  " % human_seconds(query["seconds"]),
             "crit" if query.get("blocked") else "warn"),
            (text, "normal"),
        ], width))

    relations = sizes.get("relations") or []
    for relation in relations[:max_tables]:
        lines.append(_truncate([
            ("  %-26s" % fit(relation["name"], 26), "dim"),
            ("%10s" % human_bytes(relation["total"]), "normal"),
            ("   idx %s" % human_bytes(relation["indexes"]), "dim"),
        ], width))
    return lines


def _duration_style(seconds, warn=1.0, crit=5.0):
    if seconds is None:
        return "dim"
    if seconds >= crit:
        return "crit"
    if seconds >= warn:
        return "warn"
    return "normal"


def routes_block(state, width, max_rows=6):
    """Slowest / heaviest routes, from the Odoo access log."""
    stats = (state or {}).get("routes")
    if not stats:
        return []

    title = [("ROUTES", "title")]
    if stats.get("available"):
        title.append(("  last " + human_seconds(stats.get("window")), "dim"))
        title.append(("  " + human_count(stats.get("requests")) + " req", "normal"))
        if stats.get("rps"):
            title.append((" %.1f/s" % stats["rps"], "dim"))
        if stats.get("errors"):
            title.append(("  %d err" % stats["errors"], "warn"))
        title.append(("  by " + SORT_LABELS.get(stats.get("sort"), "total time"),
                      "label"))
    lines = [_truncate(title, width)]

    if not stats.get("available"):
        reason = stats.get("error") or "unavailable"
        detail = "  " + reason
        if reason == "no log file":
            detail = "  no access log: this instance has no --logfile"
        lines.append(_truncate([(detail, "warn")], width))
        return lines

    rows = stats.get("rows") or []
    if not rows:
        lines.append(_truncate(
            [("  " + (stats.get("note") or "no requests in the window"), "dim")],
            width))
        return lines

    wide = width >= 92
    medium = width >= 74
    head = "  %6s %8s" % ("CALLS", "AVG")
    if medium:
        head += " %8s" % "P95"
    head += " %8s" % "MAX"
    if wide:
        head += " %7s %5s" % ("SQL/req", "SQL%")
    head += "  ROUTE"
    lines.append([(fit(head, width), "label")])

    for row in rows[:max_rows]:
        line = [("  %6s " % human_count(row["calls"]), "normal"),
                ("%8s" % human_duration(row["avg"]), _duration_style(row["avg"]))]
        if medium:
            line.append((" %8s" % human_duration(row["p95"]),
                         _duration_style(row["p95"])))
        line.append((" %8s" % human_duration(row["max"]),
                     _duration_style(row["max"])))
        if wide:
            line.append((" %7.0f" % row["queries"], "dim"))
            line.append((" %5s" % human_percent(row.get("sql_share")), "dim"))
        label = row["route"]
        if row.get("errors"):
            label += "  (%d err)" % row["errors"]
        line.append(("  " + label, "normal"))
        lines.append(_truncate(line, width))

    hidden = stats.get("distinct", 0) - len(rows[:max_rows])
    if hidden > 0:
        lines.append(_truncate(
            [("  ... %d more route(s)" % hidden, "dim")], width))
    return lines


def io_block(snapshot, width):
    system = snapshot.get("system") or {}
    disk_io = system.get("disk_io") or {}
    network = system.get("network") or {}
    iowait = (system.get("cpu") or {}).get("iowait")
    ops = ""
    if disk_io.get("read_ops") is not None:
        ops = "(%.0f/%.0f iops)" % (disk_io.get("read_ops") or 0,
                                    disk_io.get("write_ops") or 0)
    return [_truncate(_join(
        [("DISK", "title")],
        _pair("R", human_rate(disk_io.get("read")), "normal"),
        _pair("W", human_rate(disk_io.get("write")), "normal"),
        [(ops, "dim")] if ops else None,
        _pair("IOWAIT", human_percent(iowait), level(iowait, 10, 25)),
        [("NET", "title")],
        _pair("RX", human_rate(network.get("rx")), "normal"),
        _pair("TX", human_rate(network.get("tx")), "normal"),
    ), width)]


def notes_block(state, config_warnings, width, max_notes=3):
    lines = []
    workers = (state or {}).get("workers") or {}
    notes = list(workers.get("notes") or [])
    notes.extend((state or {}).get("warnings") or [])
    notes.extend(config_warnings or [])
    for note in notes[:max_notes]:
        lines.append(_truncate([("  ! ", "warn"), (note, "dim")], width))
    return lines


def footer(snapshot, width, paused=False):
    age = None
    if snapshot.get("time"):
        age = time.time() - snapshot["time"]
    keys = [("q", "key"), ("uit ", "dim"), ("r", "key"), ("efresh ", "dim"),
            ("l", "key"), ("ive ", "dim"), ("s", "key"), ("taging ", "dim"),
            ("tab", "key"), (" next ", "dim"), ("p", "key"), ("ause ", "dim"),
            ("t", "key"), (" sort ", "dim"), ("?", "key"), (" help", "dim")]
    right = "sample %s ago" % human_seconds(age, na="-")
    if paused:
        right = "PAUSED  " + right
    padding = width - _text_width(keys) - len(right)
    if padding > 0:
        keys.append((" " * padding, "normal"))
    keys.append((right, "warn" if paused else "dim"))
    return [_truncate(keys, width)]


HELP_TEXT = [
    ("OTOP -- htop/btop for Odoo", "title"),
    ("", "normal"),
    ("KEYS", "label"),
    ("  q / Ctrl-C   quit", "normal"),
    ("  r            refresh now (also re-measures filestore and database size)",
     "normal"),
    ("  l / s        switch to the LIVE / STAGING instance", "normal"),
    ("  1..9         switch to instance by position", "normal"),
    ("  tab          next instance", "normal"),
    ("  p            pause / resume sampling", "normal"),
    ("  t            sort ROUTES by total time / slowest / average / calls",
     "normal"),
    ("  ?            close this help", "normal"),
    ("", "normal"),
    ("ROUTES", "label"),
    ("  Read from the instance's own access log, which Odoo writes with the",
     "normal"),
    ("  SQL time and the Python time of every finished request.  RPC calls are",
     "normal"),
    ("  grouped by model.method, other requests by URL with ids folded to '*'.",
     "normal"),
    ("  AVG/P95/MAX are per request; SQL% is the share of the time spent in the",
     "normal"),
    ("  database.  Requests killed by limit_time_real never reach the log, so a",
     "normal"),
    ("  timing-out route can be missing here -- watch QUEUED as well.", "normal"),
    ("  Needs --logfile (or logfile=) and the default INFO log level.", "normal"),
    ("", "normal"),
    ("WORKER STATUS", "label"),
    ("  * BUSY       the worker owns an established connection on the Odoo HTTP",
     "normal"),
    ("               port, or has an active query -- observed, not guessed",
     "normal"),
    ("  o IDLE       no connection and no active query at the moment of sampling",
     "normal"),
    ("  ~            approximated: prefixing a status means it came from CPU use",
     "normal"),
    ("               (otop could not read /proc/<pid>/fd); after a role it means",
     "normal"),
    ("               the worker type was inferred, not confirmed", "normal"),
    ("  BUSY%        share of the last samples in which the worker was busy --",
     "normal"),
    ("               use this, not BUSY, to judge saturation", "normal"),
    ("  QUEUED       connections accepted by the kernel that no worker has picked",
     "normal"),
    ("               up yet: > 0 means every worker is busy", "normal"),
    ("", "normal"),
    ("CORES          one character per core, ' ' idle .:-=+*#%@ increasingly busy",
     "normal"),
    ("SIZES          database size and filestore size are cached; the age is shown",
     "normal"),
]


def help_lines(width):
    return [[(fit(text, width), style)] for text, style in HELP_TEXT]


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------
def build(snapshot, config, active, width, height, version="", paused=False,
          show_help=False):
    """Return the full screen as a list of lines of (text, style) segments."""
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return [_truncate([("terminal too small", "err")], width),
                _truncate([("need at least %dx%d" % (MIN_WIDTH, MIN_HEIGHT),
                            "dim")], width)]

    top = header(snapshot, config, active, width, version)
    bottom = footer(snapshot, width, paused)
    if show_help:
        body = help_lines(width - 1)
        return top + [[]] + body[:max(0, height - len(top) - len(bottom) - 1)] + bottom

    state = (snapshot.get("instances") or {}).get(active) or {}
    budget = height - len(top) - len(bottom)

    max_workers, max_queries, max_tables, max_storage, max_routes = 12, 3, 3, 8, 6
    for _attempt in range(20):
        body = []
        body.extend(system_block(snapshot, width))
        body.append([])
        body.extend(workers_block(state, width, max_workers))
        if max_routes:
            routes = routes_block(state, width, max_routes)
            if routes:
                body.append([])
                body.extend(routes)
        body.append([])
        body.extend(storage_block(snapshot, width, max_storage))
        body.append([])
        body.extend(postgres_block(state, width, max_queries, max_tables))
        notes = notes_block(state, config.warnings, width)
        if notes:
            body.append([])
            body.extend(notes)
        body.append([])
        body.extend(io_block(snapshot, width))
        if len(body) <= budget:
            break
        if max_workers > 6:
            max_workers -= 2
        elif max_routes > 3:
            max_routes -= 1
        elif max_tables:
            max_tables -= 1
        elif max_queries:
            max_queries -= 1
        elif max_storage > 3:
            max_storage -= 1
        elif max_workers > 3:
            max_workers -= 1
        elif max_routes:
            max_routes -= 1
        else:
            body = body[:budget]
            break
    body = body[:max(0, budget)]
    return top + body + bottom


def to_text(lines):
    """Plain text rendering, used by ``otop --once``."""
    return "\n".join("".join(text for text, _style in line) for line in lines)


# ---------------------------------------------------------------------------
# curses painting
# ---------------------------------------------------------------------------
def init_colors(curses):
    """Return {style: attribute}; degrades to plain attributes without colour."""
    attributes = dict.fromkeys(STYLES, 0)
    attributes["title"] = curses.A_BOLD
    attributes["label"] = curses.A_BOLD
    attributes["dim"] = curses.A_DIM
    attributes["tab_active"] = curses.A_REVERSE | curses.A_BOLD
    attributes["key"] = curses.A_BOLD
    if not curses.has_colors():
        return attributes
    curses.start_color()
    try:
        curses.use_default_colors()
        background = -1
    except curses.error:                                # pragma: no cover
        background = curses.COLOR_BLACK
    definitions = [
        (1, curses.COLOR_GREEN), (2, curses.COLOR_YELLOW), (3, curses.COLOR_RED),
        (4, curses.COLOR_CYAN), (5, curses.COLOR_BLUE), (6, curses.COLOR_MAGENTA),
    ]
    for index, colour in definitions:
        try:
            curses.init_pair(index, colour, background)
        except curses.error:                            # pragma: no cover
            pass
    attributes["good"] = curses.color_pair(1)
    attributes["idle"] = curses.color_pair(1)
    attributes["warn"] = curses.color_pair(2)
    attributes["crit"] = curses.color_pair(3) | curses.A_BOLD
    attributes["busy"] = curses.color_pair(3) | curses.A_BOLD
    attributes["err"] = curses.color_pair(3)
    attributes["title"] = curses.color_pair(4) | curses.A_BOLD
    attributes["label"] = curses.color_pair(5) | curses.A_BOLD
    attributes["key"] = curses.color_pair(4) | curses.A_BOLD
    attributes["tab_active"] = curses.color_pair(4) | curses.A_REVERSE | curses.A_BOLD
    return attributes


def paint(window, lines, attributes, curses):
    window.erase()
    height, width = window.getmaxyx()
    for row, line in enumerate(lines[:height]):
        column = 0
        for text, style in line:
            if column >= width - 1:
                break
            chunk = text[:max(0, width - 1 - column)]
            if not chunk:
                continue
            try:
                window.addstr(row, column, chunk, attributes.get(style, 0))
            except curses.error:                        # bottom-right corner
                pass
            column += len(chunk)
    window.noutrefresh()
