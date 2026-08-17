import os
import tempfile
import unittest
from collections import deque

from otop import workers
from otop.config import Instance

# A trimmed /proc/net/tcp: one LISTEN socket on port 8069 (0x1F85) with two
# connections waiting in the accept queue, one ESTABLISHED connection to that
# port, and one ESTABLISHED connection to PostgreSQL (5432 = 0x1538).
PROC_NET_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1F85 00000000:0000 0A 00000000:00000002 00:00000000 00000000  1000        0 111 1 0 20 0
   1: 0100007F:1F85 0100007F:B2A4 01 00000000:00000000 00:00000000 00000000  1000        0 222 1 0 20 0
   2: 0100007F:C350 0100007F:1538 01 00000000:00000000 00:00000000 00000000  1000        0 333 1 0 20 0
   3: 0100007F:1F85 0100007F:B2A5 06 00000000:00000000 00:00000000 00000000  1000        0 0 1 0 20 0
"""


class SocketsTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp()
        with os.fdopen(handle, "w") as file_:
            file_.write(PROC_NET_TCP)
        self.addCleanup(os.unlink, self.path)
        self.sockets = workers.Sockets.read((self.path,))

    def test_established_sockets_are_indexed_by_inode(self):
        self.assertEqual(self.sockets.by_inode[222], (8069, 45732, "01"))
        self.assertEqual(self.sockets.by_inode[333], (50000, 5432, "01"))

    def test_listen_backlog_is_the_queue_depth(self):
        self.assertEqual(self.sockets.backlog[8069], 2)

    def test_listening_socket_is_not_an_inode_connection(self):
        self.assertNotIn(111, self.sockets.by_inode)

    def test_missing_file_is_not_fatal(self):
        empty = workers.Sockets.read(("/proc/definitely/not/here",))
        self.assertEqual(empty.by_inode, {})
        self.assertEqual(empty.backlog, {})


class ClassifyTest(unittest.TestCase):
    def test_proctitle_gives_exact_types(self):
        self.assertEqual(workers.classify("odoo: WorkerHTTP 1234 "),
                         (workers.ROLE_HTTP, "", True))
        self.assertEqual(workers.classify("odoo: WorkerCron 1234 odoo_live"),
                         (workers.ROLE_CRON, "odoo_live", True))

    def test_gevent_process_is_recognised(self):
        role, _detail, exact = workers.classify(
            "/opt/odoo/venv/bin/python odoo-bin gevent -c /etc/odoo.conf")
        self.assertEqual(role, workers.ROLE_GEVENT)
        self.assertTrue(exact)

    def test_plain_worker_is_unknown_without_setproctitle(self):
        role, _detail, exact = workers.classify(
            "/opt/odoo/venv/bin/python odoo-bin -c /etc/odoo.conf")
        self.assertEqual(role, workers.ROLE_UNKNOWN)
        self.assertFalse(exact)

    def test_looks_like_odoo_filters_unrelated_processes(self):
        self.assertTrue(workers.looks_like_odoo("python3 /opt/odoo/odoo-bin -c x.conf"))
        self.assertTrue(workers.looks_like_odoo("/usr/bin/odoo -c /etc/odoo.conf"))
        self.assertTrue(workers.looks_like_odoo("odoo: WorkerHTTP 12 "))
        self.assertFalse(workers.looks_like_odoo("grep -r odoo-bin /etc/odoo.conf"))
        self.assertFalse(workers.looks_like_odoo("vim /etc/odoo.conf"))
        self.assertFalse(workers.looks_like_odoo(""))


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.sampler = workers.WorkerSampler(busy_cpu_threshold=20.0)

    def decide(self, **overrides):
        process = {"role": workers.ROLE_HTTP, "http_conns": 0, "cpu": 0.0,
                   "db_state": None, "status": "unknown", "evidence": "none",
                   "status_exact": True}
        process.update(overrides)
        self.sampler._decide_status(process, overrides.pop("_active", []))
        return process

    def test_established_connection_means_busy(self):
        process = self.decide(http_conns=1, cpu=0.0)
        self.assertEqual(process["status"], "busy")
        self.assertEqual(process["evidence"], "socket")
        self.assertTrue(process["status_exact"])

    def test_no_connection_means_idle_even_at_high_cpu(self):
        process = self.decide(http_conns=0, cpu=99.0)
        self.assertEqual(process["status"], "idle")
        self.assertEqual(process["evidence"], "socket")

    def test_cpu_is_only_used_when_sockets_are_unreadable(self):
        busy = self.decide(http_conns=None, cpu=55.0)
        self.assertEqual(busy["status"], "busy")
        self.assertEqual(busy["evidence"], "cpu")
        self.assertFalse(busy["status_exact"])

        idle = self.decide(http_conns=None, cpu=1.0)
        self.assertEqual(idle["status"], "idle")
        self.assertFalse(idle["status_exact"])

    def test_active_query_makes_a_cron_worker_busy(self):
        process = {"role": workers.ROLE_CRON, "http_conns": 0, "cpu": 0.0,
                   "db_state": "active", "status": "unknown", "evidence": "none",
                   "status_exact": True}
        self.sampler._decide_status(process, [{"state": "active"}])
        self.assertEqual(process["status"], "busy")
        self.assertEqual(process["evidence"], "database")

    def test_master_has_no_busy_state(self):
        process = self.decide(role=workers.ROLE_MASTER, http_conns=3)
        self.assertEqual(process["status"], "n/a")

    def test_nothing_observable_stays_unknown(self):
        process = self.decide(http_conns=None, cpu=None, role=workers.ROLE_UNKNOWN)
        self.assertEqual(process["status"], "unknown")


class InferRolesTest(unittest.TestCase):
    def setUp(self):
        self.sampler = workers.WorkerSampler()
        self.instance = Instance("live")
        self.instance.workers = 2
        self.instance.max_cron_threads = 1

    @staticmethod
    def row(pid, backends=()):
        return [pid, "odoo-bin -c /etc/odoo.conf", workers.ROLE_UNKNOWN, "",
                False, list(backends)]

    def test_maintenance_database_connection_identifies_the_cron_worker(self):
        rows = [self.row(10), self.row(11),
                self.row(12, [{"datname": "postgres", "state": "idle"}])]
        notes = []
        self.sampler._infer_roles(rows, self.instance, notes)
        self.assertEqual(rows[2][2], workers.ROLE_CRON)
        self.assertTrue(rows[2][4], "cron identified from postgres connection is exact")
        self.assertEqual([rows[0][2], rows[1][2]], [workers.ROLE_HTTP] * 2)
        self.assertFalse(rows[0][4], "spawn-order typing must be flagged as inferred")

    def test_spawn_order_fallback_uses_configured_counts(self):
        rows = [self.row(pid) for pid in (10, 11, 12)]
        notes = []
        self.sampler._infer_roles(rows, self.instance, notes)
        self.assertEqual([row[2] for row in rows],
                         [workers.ROLE_HTTP, workers.ROLE_HTTP, workers.ROLE_CRON])
        self.assertTrue(all(row[4] is False for row in rows))

    def test_mismatched_count_leaves_roles_unknown_with_a_note(self):
        rows = [self.row(pid) for pid in (10, 11, 12, 13, 14)]
        notes = []
        self.sampler._infer_roles(rows, self.instance, notes)
        self.assertTrue(all(row[2] == workers.ROLE_UNKNOWN for row in rows))
        self.assertTrue(notes)

    def test_unknown_counts_produce_a_note(self):
        self.instance.workers = None
        rows = [self.row(10)]
        notes = []
        self.sampler._infer_roles(rows, self.instance, notes)
        self.assertEqual(rows[0][2], workers.ROLE_UNKNOWN)
        self.assertTrue(notes)


class SampleTest(unittest.TestCase):
    def test_instance_that_is_not_running_reports_cleanly(self):
        sampler = workers.WorkerSampler()
        instance = Instance("live")
        instance.process_match = "/this/does/not/run/odoo-bin"
        result = sampler.sample(instance, workers.Sockets(), {})
        self.assertFalse(result["running"])
        self.assertEqual(result["error"], "not running")
        self.assertEqual(result["processes"], [])

    def test_instance_without_process_match_is_reported(self):
        sampler = workers.WorkerSampler()
        result = sampler.sample(Instance("live"), workers.Sockets(), {})
        self.assertIn("process_match", result["error"])

    def test_own_process_is_readable(self):
        """The /proc primitives work on ourselves (sanity check on real /proc)."""
        pid = os.getpid()
        stat = workers.read_stat(pid)
        self.assertIsNotNone(stat)
        self.assertGreaterEqual(stat["threads"], 1)
        self.assertIsNotNone(workers.read_rss(pid))
        self.assertIsInstance(workers.socket_inodes(pid), set)

    def test_unreadable_process_returns_none_not_empty(self):
        self.assertIsNone(workers.read_stat(999999))
        self.assertIsNone(workers.socket_inodes(999999))


if __name__ == "__main__":
    unittest.main()


class MultiInstanceStateTest(unittest.TestCase):
    """Regression: one sampler serves every instance, so per-instance pruning
    wiped the other instance's CPU baseline and busy history on every pass and
    CPU%/BUSY% stayed blank whenever two instances were configured."""

    def test_cpu_baseline_survives_another_instances_sample(self):
        sampler = workers.WorkerSampler()
        now = 1000.0
        self.assertIsNone(sampler._cpu_percent(4242, 100, now))     # first sight
        # a sample for a different instance, containing entirely different pids
        sampler._update_history({"processes": [{"pid": 777, "status": "idle"}],
                                 "master": {"pid": 776}})
        self.assertEqual(sampler._cpu_percent(4242, 100 + workers.TICKS, now + 1),
                         100.0)

    def test_busy_history_survives_another_instances_sample(self):
        sampler = workers.WorkerSampler()
        first = {"pid": 10, "status": "busy"}
        sampler._update_history({"processes": [first], "master": None})
        sampler._update_history({"processes": [{"pid": 20, "status": "idle"}],
                                 "master": None})
        again = {"pid": 10, "status": "idle"}
        sampler._update_history({"processes": [again], "master": None})
        self.assertEqual(again["window_samples"], 2, "history was reset")
        self.assertEqual(again["busy_ratio"], 50.0)

    def test_dead_processes_are_eventually_swept(self):
        sampler = workers.WorkerSampler()
        sampler._cpu[999999] = (1, 1.0)
        sampler._history[999999] = deque([1])
        sampler._cpu[os.getpid()] = (1, 1.0)
        for _ in range(60):
            sampler._sweep()
        self.assertNotIn(999999, sampler._cpu)
        self.assertNotIn(999999, sampler._history)
        self.assertIn(os.getpid(), sampler._cpu, "a live process must be kept")
