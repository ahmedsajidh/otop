"""Entry point: argument parsing, the sampling thread and the curses loop.

Sampling runs in one background thread so that the interface always reacts to a
key press immediately, even while a database is timing out.  Expensive figures
(filestore size, database size, table sizes) are refreshed on their own, much
slower schedules -- never once per frame.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

from . import __version__, storage, ui
from . import config as config_module
from .postgres import PostgresClient
from .system import SystemSampler
from .workers import Sockets, WorkerSampler


class Collector(threading.Thread):
    """Background sampler.  Never raises out of run()."""

    daemon = True

    def __init__(self, config):
        super().__init__(name="otop-collector")
        self.config = config
        self._snapshot = {"time": 0.0, "ready": False, "instances": {}}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.paused = False

        self.system = SystemSampler()
        self.workers = WorkerSampler(
            busy_cpu_threshold=config.busy_cpu_threshold,
            window=max(5, int(60 / max(0.5, config.refresh))),
            discovery_refresh=config.discovery_refresh,
        )
        self._postgres = {}
        self._filestores = {}
        self._sizes = {}
        self._size_time = {}
        self._relation_time = {}
        for instance in config.instances:
            self._postgres[instance.key] = PostgresClient(
                instance, config.long_query_seconds, config.show_query_text)
            self._filestores[instance.key] = storage.DirectorySize(
                instance.filestore, config.filestore_refresh)
            self._sizes[instance.key] = {"database": None, "relations": []}
            self._size_time[instance.key] = 0.0
            self._relation_time[instance.key] = 0.0

    # -- public ---------------------------------------------------------
    def snapshot(self):
        with self._lock:
            return self._snapshot

    def stop(self):
        self._stop.set()
        self._wake.set()
        for client in self._postgres.values():
            client.close()

    def refresh_now(self):
        """'r' key: drop the slow caches and take a sample immediately."""
        for key in self._size_time:
            self._size_time[key] = 0.0
            self._relation_time[key] = 0.0
        for cache in self._filestores.values():
            cache.refresh(force=True)
        self._wake.set()

    def toggle_pause(self):
        self.paused = not self.paused
        if not self.paused:
            self._wake.set()
        return self.paused

    # -- loop -----------------------------------------------------------
    def run(self):
        while not self._stop.is_set():
            started = time.time()
            if not self.paused:
                try:
                    sample = self.sample(started)
                    with self._lock:
                        self._snapshot = sample
                except Exception as exc:                # noqa: BLE001
                    with self._lock:
                        self._snapshot = {
                            "time": time.time(), "ready": False, "instances": {},
                            "error": "%s: %s" % (exc.__class__.__name__, exc),
                        }
            delay = max(0.2, self.config.refresh - (time.time() - started))
            self._wake.wait(delay)
            self._wake.clear()

    def sample(self, now=None):
        now = now or time.time()
        snapshot = {
            "time": now,
            "ready": True,
            "error": None,
            "system": self.system.sample(now),
            "disk": storage.disk_usage(self.config.disk_path),
            "instances": {},
        }
        sockets = Sockets.read()
        entries = []
        for instance in self.config.instances:
            state = self._instance(instance, sockets, now)
            snapshot["instances"][instance.key] = state
            entries.append({"label": "%s database" % instance.name,
                            "kind": "database",
                            "bytes": state["sizes"]["database"], "path": None})
            filestore = state["filestore"]
            entries.append({"label": "%s filestore" % instance.name,
                            "kind": "filestore", "bytes": filestore.get("bytes"),
                            "path": filestore.get("path"),
                            "age": filestore.get("age"),
                            "scanning": filestore.get("scanning"),
                            "error": filestore.get("error")})
        snapshot["storage"] = storage.compose(snapshot["disk"], entries,
                                              self.config.pg_data_dir)
        return snapshot

    def _instance(self, instance, sockets, now):
        client = self._postgres[instance.key]
        activity, backends = client.activity()

        try:
            workers = self.workers.sample(instance, sockets, backends, now)
        except Exception as exc:                        # noqa: BLE001
            workers = {"running": False, "mode": "unknown", "notes": [],
                       "processes": [], "master": None,
                       "error": "%s: %s" % (exc.__class__.__name__, exc)}

        sizes = self._sizes[instance.key]
        if now - self._size_time[instance.key] >= self.config.slow_refresh:
            self._size_time[instance.key] = now
            sizes["database"] = client.database_size()
        if now - self._relation_time[instance.key] >= self.config.table_refresh:
            self._relation_time[instance.key] = now
            sizes["relations"] = client.relation_sizes()

        cache = self._filestores[instance.key]
        cache.refresh()

        return {
            "key": instance.key,
            "name": instance.name,
            "database": instance.database,
            "http_port": instance.http_port,
            "warnings": list(instance.warnings),
            "workers": workers,
            "postgres": activity,
            "sizes": dict(sizes),
            "filestore": cache.snapshot(),
        }


# ---------------------------------------------------------------------------
# interactive loop
# ---------------------------------------------------------------------------
def _select(config, current, key):
    """Instance key for a pressed letter/digit, or None."""
    keys = [instance.key for instance in config.instances]
    if not keys:
        return None
    if key in ("\t", "KEY_BTAB"):
        return keys[(keys.index(current) + 1) % len(keys)] if current in keys else keys[0]
    if key.isdigit():
        index = int(key) - 1
        return keys[index] if 0 <= index < len(keys) else None
    for instance in config.instances:
        if instance.key.lower().startswith(key) or instance.name.lower().startswith(key):
            return instance.key
    return None


def run_curses(stdscr, config, collector, active, refresh_ms=250):
    import curses

    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(refresh_ms)
    attributes = ui.init_colors(curses)
    show_help = False

    while True:
        height, width = stdscr.getmaxyx()
        lines = ui.build(collector.snapshot(), config, active, width, height,
                         version=__version__, paused=collector.paused,
                         show_help=show_help)
        ui.paint(stdscr, lines, attributes, curses)
        curses.doupdate()

        try:
            pressed = stdscr.get_wch()
        except curses.error:                            # timeout, just redraw
            continue
        except KeyboardInterrupt:
            return

        if isinstance(pressed, int):
            if pressed == curses.KEY_RESIZE:
                continue
            pressed = chr(pressed) if 0 <= pressed < 0x110000 else ""
        pressed = str(pressed)

        if pressed in ("q", "Q"):
            return
        if pressed in ("?", "h", "H"):
            show_help = not show_help
            continue
        if show_help:
            show_help = False
            continue
        if pressed in ("r", "R"):
            collector.refresh_now()
            continue
        if pressed in ("p", "P"):
            collector.toggle_pause()
            continue
        chosen = _select(config, active, pressed.lower())
        if chosen:
            active = chosen


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="otop",
        description="htop/btop for Odoo: workers, PostgreSQL, storage and "
                    "server resources in the terminal.")
    parser.add_argument("-c", "--config", metavar="PATH",
                        help="configuration file (default: $%s, %s, then %s)"
                             % (config_module.CONFIG_ENV, config_module.USER_CONFIG,
                                config_module.SYSTEM_CONFIG))
    parser.add_argument("-i", "--instance", metavar="KEY",
                        help="instance to show first (default: the first one)")
    parser.add_argument("-n", "--interval", type=float, metavar="SECONDS",
                        help="refresh interval for cheap metrics")
    parser.add_argument("-1", "--once", action="store_true",
                        help="print one snapshot as plain text and exit")
    parser.add_argument("--width", type=int, default=None,
                        help="line width for --once (default: terminal width)")
    parser.add_argument("-V", "--version", action="version",
                        version="otop " + __version__)
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)

    try:
        config = config_module.load(arguments.config)
    except config_module.ConfigError as exc:
        sys.stderr.write("otop: %s\n" % exc)
        sys.stderr.write("Create %s -- see /usr/share/doc/otop/config.example.yaml "
                         "or the README.\n" % config_module.SYSTEM_CONFIG)
        return 2

    if arguments.interval:
        config.refresh = max(0.5, arguments.interval)

    active = arguments.instance or config.instances[0].key
    if config.instance(active) is None:
        sys.stderr.write("otop: no instance named %r (configured: %s)\n"
                         % (active, ", ".join(i.key for i in config.instances)))
        return 2

    collector = Collector(config)

    if arguments.once:
        collector.sample()                              # prime the rate counters
        time.sleep(min(1.0, config.refresh))
        snapshot = collector.sample()
        width = arguments.width or _terminal_width()
        lines = ui.build(snapshot, config, active, width, 200,
                         version=__version__)
        print(ui.to_text(lines))
        collector.stop()
        return 0

    if not sys.stdout.isatty():
        sys.stderr.write("otop: not a terminal; use --once for plain output\n")
        collector.stop()
        return 2

    collector.start()
    deadline = time.time() + 2.0
    while not collector.snapshot().get("ready") and time.time() < deadline:
        time.sleep(0.05)

    import curses
    try:
        curses.wrapper(run_curses, config, collector, active)
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
    return 0


def _terminal_width(default=100):
    try:
        return max(60, os.get_terminal_size().columns)
    except OSError:
        return default


if __name__ == "__main__":                              # pragma: no cover
    raise SystemExit(main())
