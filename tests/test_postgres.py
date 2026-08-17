import unittest

from otop.config import Instance
from otop.postgres import PostgresClient, truncate_query


def row(pid, datname, state, wait=None, port=None, query_age=None, xact_age=None,
        query="SELECT 1", backend_type="client backend"):
    return (pid, datname, state, wait, backend_type, port, query_age, xact_age, query)


class FakeClient(PostgresClient):
    """PostgresClient with the database replaced by a canned result set."""

    def __init__(self, rows, **kwargs):
        instance = Instance("live")
        instance.database = "odoo_live"
        super().__init__(instance, **kwargs)
        self.rows = rows

    def _fetch(self, sql, params=None):
        return self.rows


class ActivityTest(unittest.TestCase):
    def test_counts_only_this_database(self):
        client = FakeClient([
            row(1, "odoo_live", "active", port=5000, query_age=1.0),
            row(2, "odoo_live", "idle", port=5001),
            row(3, "odoo_live", "idle in transaction", port=5002, xact_age=1.0),
            row(4, "other_db", "active", port=5003, query_age=99.0),
        ])
        summary, by_port = client.activity()
        self.assertTrue(summary["available"])
        self.assertEqual(summary["connections"], 3)
        self.assertEqual(summary["active"], 1)
        self.assertEqual(summary["idle"], 1)
        self.assertEqual(summary["idle_in_transaction"], 1)
        self.assertEqual(summary["longest"], 1.0)
        self.assertEqual(sorted(by_port), [5000, 5001, 5002, 5003])

    def test_backends_of_other_databases_are_still_mapped_for_workers(self):
        """A cron worker's connection is to `postgres`, not to the Odoo database."""
        client = FakeClient([row(9, "postgres", "idle", port=6000)])
        summary, by_port = client.activity()
        self.assertEqual(summary["connections"], 0)
        self.assertEqual(by_port[6000]["datname"], "postgres")

    def test_background_workers_are_ignored(self):
        client = FakeClient([
            row(1, "odoo_live", None, backend_type="autovacuum worker", port=1),
            row(2, "odoo_live", "idle", port=2),
        ])
        summary, _ = client.activity()
        self.assertEqual(summary["connections"], 1)
        self.assertFalse(summary["restricted"])

    def test_hidden_state_is_reported_as_restricted(self):
        client = FakeClient([row(1, "odoo_live", None, port=1)])
        summary, _ = client.activity()
        self.assertTrue(summary["restricted"])

    def test_lock_waits_are_counted_separately(self):
        client = FakeClient([
            row(1, "odoo_live", "active", wait="Lock", port=1, query_age=3.0),
            row(2, "odoo_live", "active", wait="ClientRead", port=2, query_age=1.0),
        ])
        summary, _ = client.activity()
        self.assertEqual(summary["active"], 2)
        self.assertEqual(summary["waiting"], 1)

    def test_long_queries_are_sorted_and_truncated(self):
        client = FakeClient([
            row(1, "odoo_live", "active", port=1, query_age=7.0, query="SELECT a"),
            row(2, "odoo_live", "active", port=2, query_age=30.0,
                query="SELECT   b\n  FROM   c"),
            row(3, "odoo_live", "active", port=3, query_age=0.5, query="SELECT d"),
        ], long_query_seconds=5.0)
        summary, _ = client.activity()
        self.assertEqual([q["pid"] for q in summary["long_queries"]], [2, 1])
        self.assertEqual(summary["long_queries"][0]["query"], "SELECT b FROM c")
        self.assertEqual(summary["longest"], 30.0)

    def test_query_text_can_be_withheld(self):
        client = FakeClient([row(1, "odoo_live", "active", port=1, query_age=9.0)],
                            show_query_text=False)
        summary, _ = client.activity()
        self.assertIsNone(summary["long_queries"][0]["query"])

    def test_unavailable_database_degrades(self):
        class Broken(FakeClient):
            def _fetch(self, sql, params=None):
                self.error = "connection refused"

        summary, by_port = Broken([]).activity()
        self.assertFalse(summary["available"])
        self.assertEqual(summary["error"], "connection refused")
        self.assertEqual(by_port, {})

    def test_instance_without_database_does_not_connect(self):
        client = PostgresClient(Instance("live"))
        self.assertIsNone(client._connect())
        self.assertIn("no database", client.error or "no psycopg driver installed")


class TruncateTest(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(truncate_query("SELECT\n  1,\t2"), "SELECT 1, 2")

    def test_limits_length(self):
        text = truncate_query("x" * 500, limit=20)
        self.assertEqual(len(text), 23)
        self.assertTrue(text.endswith("..."))

    def test_none(self):
        self.assertIsNone(truncate_query(None))


if __name__ == "__main__":
    unittest.main()
