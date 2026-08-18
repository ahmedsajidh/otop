"""Access-log parsing, tailing and aggregation.

The sample lines are copied verbatim from a production Odoo 19 log.
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from otop import routes                                 # noqa: E402

RPC = ('2026-07-30 18:36:11,465 2370781 INFO live-linkserve.cloudpepper.site '
       'werkzeug: 43.231.28.178 - - [30/Jul/2026 18:36:11] "POST '
       '/web/dataset/call_kw/stock.picking.type/web_search_read'
       '#stock.picking.type.web_search_read HTTP/1.1" 200 - 54 0.035 0.054')
ASSET = ('2026-07-30 18:36:12,070 2370782 INFO live-linkserve.cloudpepper.site '
         'werkzeug: 43.231.28.178 - - [30/Jul/2026 18:36:12] "GET '
         '/web/assets/b19d673/web.chartjs_lib.min.js HTTP/1.1" 200 - 12 0.013 0.033')
POS = ('2026-08-18 06:12:02,001 4242 INFO staging-linkserve.cloudpepper.site '
       'werkzeug: 10.0.0.9 - - [18/Aug/2026 06:12:02] "POST '
       '/web/dataset/call_kw/pos.session/load_data#pos.session.load_data '
       'HTTP/1.1" 200 - 412 41.998 17.246')
NOPERF = ('2026-08-18 06:12:03,001 4242 INFO staging-linkserve.cloudpepper.site '
          'werkzeug: 10.0.0.9 - - [18/Aug/2026 06:12:03] "GET /web/login '
          'HTTP/1.1" 200 - - - -')
OTHER = ('2026-08-17 17:58:40,955 1232 INFO live-linkserve.cloudpepper.site '
         'odoo.addons.base.models.ir_cron: Job done')


class ParseTest(unittest.TestCase):
    def test_rpc_line(self):
        request = routes.parse_line(RPC)
        self.assertEqual(request["route"], "stock.picking.type.web_search_read")
        self.assertEqual(request["queries"], 54)
        self.assertAlmostEqual(request["query_time"], 0.035)
        self.assertAlmostEqual(request["total"], 0.089)
        self.assertEqual(request["status"], 200)
        self.assertEqual(request["pid"], 2370781)
        self.assertEqual(request["database"], "live-linkserve.cloudpepper.site")

    def test_asset_hash_is_folded(self):
        self.assertEqual(routes.parse_line(ASSET)["route"],
                         "GET /web/assets/*/web.chartjs_lib.min.js")

    def test_missing_perf_info_is_not_a_timing(self):
        request = routes.parse_line(NOPERF)
        self.assertIsNotNone(request)
        self.assertIsNone(request["total"])

    def test_non_request_lines_are_ignored(self):
        self.assertIsNone(routes.parse_line(OTHER))
        self.assertIsNone(routes.parse_line(""))
        self.assertIsNone(routes.parse_line("garbage"))

    def test_timestamp(self):
        request = routes.parse_line(RPC)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(request["time"])),
                         "2026-07-30 18:36:11")

    def test_stamp_cache_handles_a_new_minute(self):
        cache = routes._StampCache()
        first = cache.to_epoch("2026-08-18", "06:12:02")
        second = cache.to_epoch("2026-08-18", "06:13:02")
        self.assertEqual(second - first, 60)

    def test_colour_codes_are_stripped(self):
        request = routes.parse_line(RPC.replace('"POST', '"\x1b[1;31mPOST'))
        self.assertIsNotNone(request)


class NormaliseTest(unittest.TestCase):
    def test_query_string_dropped(self):
        self.assertEqual(routes.normalise("GET", "/web/bundle/web.assets?lang=en"),
                         "GET /web/bundle/web.assets")

    def test_numeric_ids_folded(self):
        self.assertEqual(routes.normalise("GET", "/web/image/product.template/42/image_128"),
                         "GET /web/image/product.template/*/image_128")

    def test_slug_ids_folded(self):
        self.assertEqual(routes.normalise("GET", "/web/content/1234-abcdef/logo.png"),
                         "GET /web/content/*/logo.png")

    def test_call_kw_without_fragment(self):
        self.assertEqual(routes.normalise("POST", "/web/dataset/call_kw/res.partner/read"),
                         "res.partner.read")

    def test_plain_route_is_left_alone(self):
        self.assertEqual(routes.normalise("POST", "/pos/ws"), "POST /pos/ws")

    def test_words_are_not_mistaken_for_ids(self):
        self.assertEqual(routes.normalise("GET", "/shop/cart"), "GET /shop/cart")


def at(line, clock, date="2026-08-18"):
    """Same log line, re-stamped -- the window is timestamp driven."""
    return date + " " + clock + line[len(date) + len(clock) + 1:]


class StatsTest(unittest.TestCase):
    def _stats(self, window=900.0):
        stats = routes.RouteStats(window)
        for line, clock in ((RPC, "06:12:00"), (ASSET, "06:12:01"),
                            (RPC, "06:12:01"), (POS, "06:12:02")):
            stats.add(routes.parse_line(at(line, clock)))
        return stats

    def test_aggregates(self):
        summary = self._stats().summary()
        self.assertEqual(summary["requests"], 4)
        self.assertEqual(summary["distinct"], 3)
        top = summary["rows"][0]
        self.assertEqual(top["route"], "pos.session.load_data")   # by total time
        self.assertAlmostEqual(top["max"], 59.244, places=3)
        self.assertAlmostEqual(top["sql_share"], 100.0 * 41.998 / 59.244, places=3)
        self.assertAlmostEqual(top["queries"], 412.0)

    def test_sorting(self):
        stats = self._stats()
        self.assertEqual(stats.summary("calls")["rows"][0]["route"],
                         "stock.picking.type.web_search_read")
        self.assertEqual(stats.summary("max")["rows"][0]["route"],
                         "pos.session.load_data")

    def test_window_is_relative_to_the_newest_line(self):
        # RPC here is weeks older than POS and must fall out of the window,
        # even though "now" is irrelevant to a log that may itself be stale.
        stats = routes.RouteStats(window=60.0)
        stats.add(routes.parse_line(RPC))               # 2026-07-30
        stats.add(routes.parse_line(POS))               # 2026-08-18
        summary = stats.summary()
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["rows"][0]["route"], "pos.session.load_data")

    def test_untimed_requests_are_counted_apart(self):
        stats = routes.RouteStats()
        stats.add(routes.parse_line(NOPERF))
        summary = stats.summary()
        self.assertEqual(summary["requests"], 0)
        self.assertEqual(summary["untimed"], 1)

    def test_errors_are_counted(self):
        stats = routes.RouteStats()
        stats.add(routes.parse_line(RPC.replace('" 200 -', '" 500 -')))
        summary = stats.summary()
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["rows"][0]["errors"], 1)

    def test_max_events_is_capped(self):
        stats = routes.RouteStats(max_events=10)
        for _index in range(50):
            stats.add(routes.parse_line(RPC))
        self.assertEqual(len(stats.events), 10)
        self.assertEqual(stats.dropped, 40)

    def test_percentile_never_indexes_past_the_end(self):
        stats = routes.RouteStats()
        stats.add(routes.parse_line(RPC))
        self.assertAlmostEqual(stats.summary()["rows"][0]["p95"], 0.089)


class TailerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "odoo-server.log")

    def tearDown(self):
        for name in os.listdir(self.directory):
            os.unlink(os.path.join(self.directory, name))
        os.rmdir(self.directory)

    def _write(self, text, mode="a"):
        with open(self.path, mode) as handle:
            handle.write(text)

    def test_reads_only_what_is_new(self):
        self._write(RPC + "\n", "w")
        tailer = routes.LogTailer(self.path, seed_bytes=1 << 20)
        self.assertEqual(len(tailer.read()), 1)
        self.assertEqual(tailer.read(), [])
        self._write(ASSET + "\n")
        self.assertEqual(len(tailer.read()), 1)

    def test_partial_line_is_held_back(self):
        self._write(RPC + "\n" + ASSET[:20], "w")
        tailer = routes.LogTailer(self.path, seed_bytes=1 << 20)
        self.assertEqual(len(tailer.read()), 1)
        self._write(ASSET[20:] + "\n")
        lines = tailer.read()
        self.assertEqual(lines, [ASSET])

    def test_copytruncate_rotation(self):
        self._write(RPC + "\n", "w")
        tailer = routes.LogTailer(self.path, seed_bytes=1 << 20)
        tailer.read()
        self._write(POS + "\n", "w")                    # logrotate copytruncate
        self.assertEqual(tailer.read(), [POS])

    def test_create_rotation(self):
        self._write(RPC + "\n", "w")
        tailer = routes.LogTailer(self.path, seed_bytes=1 << 20)
        tailer.read()
        os.rename(self.path, self.path + ".1")          # logrotate create
        self._write(POS + "\n", "w")
        self.assertEqual(tailer.read(), [POS])

    def test_seeded_start_drops_the_first_fragment(self):
        self._write((RPC + "\n") * 20, "w")
        tailer = routes.LogTailer(self.path, seed_bytes=len(RPC) + 20)
        lines = tailer.read()
        self.assertTrue(all(line == RPC for line in lines), lines)

    def test_missing_file(self):
        tailer = routes.LogTailer(self.path)
        self.assertEqual(tailer.read(), [])
        self.assertEqual(tailer.error, "log file not found")

    def test_no_path_configured(self):
        tailer = routes.LogTailer(None)
        self.assertEqual(tailer.read(), [])
        self.assertEqual(tailer.error, "no log file")

    def test_appears_later(self):
        tailer = routes.LogTailer(self.path)
        tailer.read()
        self._write(RPC + "\n", "w")
        self.assertEqual(tailer.read(), [RPC])


class WatcherTest(unittest.TestCase):
    class _Instance:
        key = "live"
        database = "live-linkserve.cloudpepper.site"
        logfile = None

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "odoo-server.log")
        with open(self.path, "w") as handle:
            handle.write(RPC + "\n" + POS + "\n")       # POS line is another db

    def tearDown(self):
        os.unlink(self.path)
        os.rmdir(self.directory)

    def test_only_our_database_is_kept(self):
        instance = self._Instance()
        instance.logfile = self.path
        watcher = routes.RouteWatcher(instance)
        summary = watcher.sample()
        self.assertTrue(summary["available"])
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["rows"][0]["route"],
                         "stock.picking.type.web_search_read")

    def test_unavailable_without_a_logfile(self):
        watcher = routes.RouteWatcher(self._Instance())
        summary = watcher.sample()
        self.assertFalse(summary["available"])
        self.assertEqual(summary["error"], "no log file")
        self.assertEqual(summary["rows"], [])

    def test_sort_cycle(self):
        self.assertEqual(routes.next_sort("total"), "max")
        self.assertEqual(routes.next_sort(routes.SORTS[-1]), "total")
        self.assertEqual(routes.next_sort("nonsense"), "total")


class DiscoverLogfileTest(unittest.TestCase):
    def test_none_without_a_pid(self):
        self.assertIsNone(routes.discover_logfile(None))

    def test_none_for_a_process_without_logfile(self):
        self.assertIsNone(routes.discover_logfile(os.getpid()))

    def test_argument_forms(self):
        self.assertEqual(routes.logfile_argument(
            ["python3", "odoo-bin", "--logfile", "logs/odoo.log"]), "logs/odoo.log")
        self.assertEqual(routes.logfile_argument(
            ["python3", "odoo-bin", "--logfile=/var/log/odoo.log"]),
            "/var/log/odoo.log")
        self.assertIsNone(routes.logfile_argument(["python3", "odoo-bin", "-c", "x"]))
        self.assertIsNone(routes.logfile_argument(["python3", "--logfile"]))

    def test_relative_path_falls_back_to_a_base_directory(self):
        """systemd drops privileges, so the master's cwd is often unreadable.

        The instance directory must then settle a relative --logfile.
        """
        directory = tempfile.mkdtemp()
        try:
            os.mkdir(os.path.join(directory, "logs"))
            wanted = os.path.join(directory, "logs", "odoo-server.log")
            open(wanted, "w").close()
            resolved = routes.discover_logfile(
                os.getpid(), cwd_pids=(), bases=[directory],
            ) if routes.logfile_argument(routes._cmdline(os.getpid())) else None
            self.assertIsNone(resolved)      # this test process has no --logfile

            # ...so exercise the resolution itself with a stub command line.
            original = routes._cmdline
            routes._cmdline = lambda pid: ["python3", "odoo-bin", "--logfile",
                                           "logs/odoo-server.log"]
            try:
                self.assertEqual(
                    routes.discover_logfile(1, cwd_pids=(), bases=[directory]),
                    wanted)
                # An unreadable cwd and a wrong base still yield a named path,
                # so the panel can report "log file not found" instead of
                # pretending the instance has no log at all.
                self.assertTrue(routes.discover_logfile(
                    1, cwd_pids=(), bases=["/nonexistent"]).endswith(
                        "logs/odoo-server.log"))
            finally:
                routes._cmdline = original
        finally:
            for root, _dirs, files in os.walk(directory, topdown=False):
                for name in files:
                    os.unlink(os.path.join(root, name))
                os.rmdir(root)

    def test_absolute_path_is_used_as_is(self):
        original = routes._cmdline
        routes._cmdline = lambda pid: ["odoo-bin", "--logfile=/var/log/odoo.log"]
        try:
            self.assertEqual(routes.discover_logfile(1), "/var/log/odoo.log")
        finally:
            routes._cmdline = original


if __name__ == "__main__":
    unittest.main()
