import unittest

from otop import ui
from otop.config import parse

SNAPSHOT = {
    "time": 1000.0,
    "ready": True,
    "system": {
        "host": {"hostname": "server", "uptime": 3600.0},
        "cpu": {"percent": 42.0, "per_core": [10.0, 90.0], "cores": 2,
                "load": [1.0, 2.0, 3.0], "iowait": 5.0},
        "memory": {"total": 100, "used": 60, "available": 40, "percent": 60.0,
                   "swap_total": 100, "swap_used": 5, "swap_percent": 5.0},
        "disk_io": {"read": 1024.0, "write": 2048.0, "read_ops": 10, "write_ops": 5},
        "network": {"rx": 512.0, "tx": 256.0},
    },
    "disk": {"path": "/", "total": 1000, "used": 600, "free": 400,
             "percent": 60.0, "device": 1, "error": None},
    "storage": {"rows": [{"label": "LIVE database", "bytes": 200, "same_fs": True},
                         {"label": "LIVE filestore", "bytes": 100, "same_fs": True,
                          "age": 30.0}],
                "other": 300, "accounted": 300},
    "instances": {
        "live": {
            "key": "live", "name": "LIVE", "database": "odoo_live",
            "http_port": 8069, "warnings": [],
            "workers": {
                "running": True, "mode": "prefork", "error": None,
                "notes": ["worker types marked ~ are inferred"],
                "master": {"pid": 100, "role": "master", "status": "n/a",
                           "cpu": 0.0, "rss": 10, "uptime": 60.0, "threads": 4,
                           "role_exact": True, "status_exact": True},
                "processes": [
                    {"pid": 101, "role": "http", "status": "busy", "cpu": 80.0,
                     "rss": 100, "uptime": 60.0, "busy_ratio": 50.0,
                     "role_exact": False, "status_exact": True, "db_state": "active",
                     "db_query_age": 1.0},
                    {"pid": 102, "role": "http", "status": "idle", "cpu": 1.0,
                     "rss": 100, "uptime": 60.0, "busy_ratio": 0.0,
                     "role_exact": True, "status_exact": False, "db_state": None},
                ],
                "http_total": 2, "busy": 1, "idle": 1, "unknown": 0,
                "utilisation": 50.0, "utilisation_avg": 25.0, "window_seconds": 30,
                "queued": 0, "in_flight": 1, "evidence": "socket",
            },
            "postgres": {"available": True, "error": None, "connections": 5,
                         "active": 1, "idle": 4, "idle_in_transaction": 0,
                         "waiting": 0, "longest": 2.5, "restricted": False,
                         "long_queries": [{"pid": 7, "seconds": 12.0,
                                           "blocked": False, "query": "SELECT 1"}]},
            "sizes": {"database": 200, "relations": [
                {"name": "mail_message", "total": 100, "indexes": 20, "heap": 80}]},
            "filestore": {"path": "/srv/fs", "bytes": 100, "files": 5, "age": 30.0,
                          "scanning": False, "error": None},
            "routes": {
                "available": True, "error": None, "note": None, "sort": "total",
                "window": 900.0, "requests": 120, "distinct": 4, "errors": 1,
                "rps": 0.13, "total_time": 176.4, "span": 900.0,
                "newest": 1000.0, "untimed": 0, "dropped": 0,
                "path": "/var/odoo/live/logs/odoo-server.log",
                "rows": [
                    {"route": "pos.session.load_data", "calls": 3, "total": 175.0,
                     "avg": 58.4, "p95": 59.2, "max": 59.244, "sql": 126.0,
                     "sql_share": 72.0, "queries": 412.0, "errors": 0},
                    {"route": "GET /web/assets/*/web.assets_backend.min.js",
                     "calls": 40, "total": 1.4, "avg": 0.034, "p95": 0.09,
                     "max": 0.3, "sql": 0.7, "sql_share": 50.0, "queries": 7.0,
                     "errors": 1},
                ],
            },
        },
    },
}

CONFIG = parse({"instances": {"live": {"name": "LIVE", "database": "odoo_live"},
                              "staging": {"name": "STAGING", "database": "odoo_stg"}}})


def render(width=100, height=40, **kwargs):
    return ui.to_text(ui.build(SNAPSHOT, CONFIG, "live", width, height,
                               version="1.0.0", **kwargs))


class LayoutTest(unittest.TestCase):
    def test_every_section_is_present(self):
        text = render()
        for expected in ("OTOP", "CPU", "RAM", "ODOO WORKERS", "STORAGE",
                         "POSTGRESQL", "DISK", "NET", "quit"):
            self.assertIn(expected, text)

    def test_total_disk_is_shown_before_the_breakdown(self):
        text = render()
        total = text.index("1000 B / ") if "1000 B / " in text else text.index("STORAGE")
        self.assertLess(total, text.index("LIVE database"))
        self.assertLess(text.index("LIVE database"), text.index("Other files"))

    def test_worker_rows_and_markers(self):
        text = render()
        self.assertIn("101", text)
        self.assertIn("BUSY", text)
        self.assertIn("IDLE", text)
        self.assertIn("http~", text)            # inferred role marker
        self.assertIn("~o IDLE", text)          # approximated status marker

    def test_lines_never_exceed_the_width(self):
        for width in (40, 60, 80, 100, 200):
            lines = ui.build(SNAPSHOT, CONFIG, "live", width, 40, version="1.0.0")
            for line in lines:
                self.assertLessEqual(sum(len(t) for t, _s in line), width)

    def test_layout_fits_the_height(self):
        for height in (10, 14, 20, 24, 40, 60):
            lines = ui.build(SNAPSHOT, CONFIG, "live", 100, height, version="1.0.0")
            self.assertLessEqual(len(lines), height)

    def test_footer_survives_a_short_terminal(self):
        text = ui.to_text(ui.build(SNAPSHOT, CONFIG, "live", 100, 14, version="1.0"))
        self.assertIn("quit", text)

    def test_tiny_terminal_is_reported_not_crashed(self):
        text = ui.to_text(ui.build(SNAPSHOT, CONFIG, "live", 20, 5, version="1.0"))
        self.assertIn("too small", text)

    def test_help_overlay(self):
        text = render(show_help=True)
        self.assertIn("KEYS", text)
        self.assertIn("BUSY%", text)

    def test_paused_marker(self):
        self.assertIn("PAUSED", render(paused=True))


class RoutesPanelTest(unittest.TestCase):
    def test_routes_are_shown_with_their_timings(self):
        text = render()
        self.assertIn("ROUTES", text)
        self.assertIn("pos.session.load_data", text)
        self.assertIn("59.2s", text)                    # MAX, sub-minute
        self.assertIn("34ms", text)                     # AVG, sub-second
        self.assertIn("by total time", text)
        self.assertIn("1 err", text)

    def test_narrow_terminal_drops_the_optional_columns(self):
        wide = render(width=120)
        self.assertIn("SQL%", wide)
        narrow = render(width=70)
        self.assertNotIn("SQL%", narrow)
        self.assertIn("pos.session.load_data", narrow)

    def test_missing_logfile_is_explained_not_hidden(self):
        snapshot = dict(SNAPSHOT)
        instance = dict(SNAPSHOT["instances"]["live"])
        instance["routes"] = {"available": False, "error": "no log file",
                              "rows": [], "distinct": 0, "requests": 0}
        snapshot["instances"] = {"live": instance}
        text = ui.to_text(ui.build(snapshot, CONFIG, "live", 100, 40, version="1.0"))
        self.assertIn("ROUTES", text)
        self.assertIn("--logfile", text)

    def test_no_route_data_means_no_panel(self):
        snapshot = dict(SNAPSHOT)
        instance = dict(SNAPSHOT["instances"]["live"])
        instance["routes"] = None
        snapshot["instances"] = {"live": instance}
        text = ui.to_text(ui.build(snapshot, CONFIG, "live", 100, 40, version="1.0"))
        self.assertNotIn("ROUTES", text)

    def test_a_very_long_route_is_clipped(self):
        snapshot = dict(SNAPSHOT)
        instance = dict(SNAPSHOT["instances"]["live"])
        stats = dict(instance["routes"])
        stats["rows"] = [dict(stats["rows"][0], route="x" * 400)]
        stats["error"] = "y" * 400
        instance["routes"] = stats
        snapshot["instances"] = {"live": instance}
        for width in (40, 60, 92, 120):
            for line in ui.build(snapshot, CONFIG, "live", width, 40, version="1.0"):
                self.assertLessEqual(sum(len(t) for t, _s in line), width)


class DegradedTest(unittest.TestCase):
    def test_empty_snapshot_still_renders(self):
        text = ui.to_text(ui.build({}, CONFIG, "live", 100, 40, version="1.0.0"))
        self.assertIn("OTOP", text)
        self.assertIn("N/A", text)

    def test_stopped_odoo_and_dead_database(self):
        snapshot = {
            "time": 1.0,
            "instances": {"live": {
                "name": "LIVE", "database": "odoo_live", "warnings": [],
                "workers": {"error": "not running", "mode": "unknown",
                            "notes": [], "processes": [], "master": None},
                "postgres": {"available": False, "error": "connection refused"},
                "sizes": {"database": None, "relations": []},
                "filestore": {"bytes": None, "error": "not found: /srv/fs"},
            }},
            "disk": {"path": "/", "error": "No such file or directory"},
            "storage": {"rows": [], "other": None},
        }
        text = ui.to_text(ui.build(snapshot, CONFIG, "live", 100, 40, version="1.0"))
        self.assertIn("not running", text)
        self.assertIn("connection refused", text)
        self.assertIn("No such file or directory", text)

    def test_unknown_instance_key_does_not_crash(self):
        text = ui.to_text(ui.build(SNAPSHOT, CONFIG, "nope", 100, 40, version="1.0"))
        self.assertIn("OTOP", text)

    def test_long_error_messages_are_still_clipped_to_the_width(self):
        """A PostgreSQL error is easily longer than the terminal is wide."""
        snapshot = {
            "time": 1.0,
            "instances": {"live": {
                "name": "LIVE", "database": "a_database_with_a_very_long_name",
                "warnings": ["a warning that goes on and on and on " * 3],
                "workers": {"error": "boom: " + "x" * 300, "mode": "unknown",
                            "notes": ["note " * 60], "processes": [], "master": None},
                "postgres": {"available": False,
                             "error": "connection to server at 127.0.0.1, port "
                                      "5432 failed: FATAL: database "
                                      "\"no_such_db\" does not exist"},
                "sizes": {"database": None, "relations": []},
                "filestore": {"bytes": None, "error": "not found: " + "/x" * 100},
            }},
            "disk": {"path": "/" + "long" * 40, "error": "No such file or directory"},
            "storage": {"rows": [{"label": "LIVE filestore", "bytes": None,
                                  "error": "not found: " + "/x" * 100}],
                        "other": None},
        }
        for width in (40, 60, 92, 120):
            for line in ui.build(snapshot, CONFIG, "live", width, 30, version="1.0"):
                self.assertLessEqual(sum(len(t) for t, _s in line), width)


if __name__ == "__main__":
    unittest.main()
