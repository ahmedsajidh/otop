"""PostgreSQL metrics for one Odoo database (read-only).

Uses psycopg2 or psycopg 3, whichever is installed; without either, the
PostgreSQL panel simply reports that the driver is missing and everything else
in otop keeps working.

``pg_stat_activity`` is read for the whole cluster rather than one database,
because that is what lets a backend be matched to the Odoo worker that owns it
(via the client port) -- including a cron worker's connection to the ``postgres``
maintenance database, which is how cron workers are identified.  Per-database
numbers are filtered afterwards.

Note on permissions: PostgreSQL hides ``state``, ``query`` and ``wait_event``
for backends owned by other roles.  When that happens otop says so instead of
reporting misleadingly low numbers.  ``GRANT pg_monitor TO <role>`` fixes it.
"""

from __future__ import annotations

import re
import time

DRIVER = None
_driver = None
try:
    import psycopg as _driver  # psycopg 3
    DRIVER = "psycopg3"
except ImportError:                                     # pragma: no cover
    try:
        import psycopg2 as _driver
        DRIVER = "psycopg2"
    except ImportError:
        _driver = None

WHITESPACE = re.compile(r"\s+")

ACTIVITY_SQL = """
SELECT pid,
       datname,
       state,
       wait_event_type,
       backend_type,
       client_port,
       EXTRACT(EPOCH FROM (now() - query_start))::float,
       EXTRACT(EPOCH FROM (now() - xact_start))::float,
       left(query, 300)
  FROM pg_stat_activity
 WHERE pid <> pg_backend_pid()
"""

SIZE_SQL = "SELECT pg_database_size(current_database())"

RELATION_SQL = """
SELECT c.relname,
       pg_total_relation_size(c.oid),
       pg_indexes_size(c.oid),
       pg_relation_size(c.oid)
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = 'r'
   AND n.nspname NOT IN ('pg_catalog', 'information_schema')
 ORDER BY 2 DESC
 LIMIT %s
"""


def truncate_query(text, limit=120):
    if not text:
        return None
    collapsed = WHITESPACE.sub(" ", text).strip()
    return collapsed[:limit] + "..." if len(collapsed) > limit else collapsed


def _first_line(exc):
    lines = str(exc).strip().splitlines()
    return lines[0][:160] if lines else exc.__class__.__name__


class PostgresClient:
    """Lazily connected, autocommit, read-only, with a reconnect back-off."""

    RETRY_SECONDS = 15.0

    def __init__(self, instance, long_query_seconds=5.0, show_query_text=True):
        self.instance = instance
        self.long_query_seconds = long_query_seconds
        self.show_query_text = show_query_text
        self.error = None
        self._connection = None
        self._retry_after = 0.0

    # -- connection -----------------------------------------------------
    def close(self):
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:                           # pragma: no cover
                pass
            self._connection = None

    def _connect(self):
        if _driver is None:
            self.error = "no psycopg driver installed"
            return None
        arguments = self.instance.connect_kwargs()
        if not arguments:
            self.error = "no database configured"
            return None
        if time.time() < self._retry_after:
            return None
        try:
            connection = _driver.connect(**arguments)
            connection.autocommit = True
            self.error = None
            return connection
        except Exception as exc:                        # noqa: BLE001
            self.error = _first_line(exc)
            self._retry_after = time.time() + self.RETRY_SECONDS
            return None

    def _fetch(self, sql, params=None):
        for attempt in (1, 2):
            if self._connection is None:
                self._connection = self._connect()
            if self._connection is None:
                return None
            try:
                with self._connection.cursor() as cursor:
                    cursor.execute(sql, params or ())
                    return cursor.fetchall()
            except Exception as exc:                    # noqa: BLE001
                self.error = _first_line(exc)
                self.close()
                if attempt == 2:
                    return None
        return None                                     # pragma: no cover

    # -- metrics --------------------------------------------------------
    def activity(self):
        """(summary dict, {client_port: backend}) for worker attribution."""
        summary = {
            "available": False,
            "driver": DRIVER,
            "error": self.error or ("no psycopg driver installed"
                                    if _driver is None else None),
            "connections": None,
            "active": None,
            "idle": None,
            "idle_in_transaction": None,
            "waiting": None,
            "longest": None,
            "long_queries": [],
            "restricted": False,
        }
        rows = self._fetch(ACTIVITY_SQL)
        if rows is None:
            summary["error"] = self.error or "unavailable"
            return summary, {}

        database = self.instance.database
        by_port = {}
        total = active = idle = idle_tx = waiting = hidden = 0
        longest = None
        long_queries = []

        for row in rows:
            (pid, datname, state, wait_type, backend_type, client_port,
             query_age, xact_age, query) = row
            if backend_type not in (None, "client backend"):
                continue                                # autovacuum, walwriter...
            if client_port and client_port > 0:
                by_port[client_port] = {"pid": pid, "datname": datname,
                                        "state": state, "query_age": query_age}
            if datname != database:
                continue
            total += 1
            if state is None:
                hidden += 1
            elif state == "active":
                active += 1
                if wait_type == "Lock":
                    waiting += 1
                if query_age is not None:
                    if longest is None or query_age > longest:
                        longest = query_age
                    if query_age >= self.long_query_seconds:
                        long_queries.append({
                            "pid": pid,
                            "seconds": query_age,
                            "blocked": wait_type == "Lock",
                            "query": (truncate_query(query)
                                      if self.show_query_text else None),
                        })
            elif state == "idle":
                idle += 1
            elif state.startswith("idle in transaction"):
                idle_tx += 1
                if xact_age is not None and xact_age >= self.long_query_seconds:
                    long_queries.append({"pid": pid, "seconds": xact_age,
                                         "blocked": False,
                                         "query": "<idle in transaction>"})

        long_queries.sort(key=lambda item: item["seconds"], reverse=True)
        summary.update({
            "available": True,
            "error": None,
            "connections": total,
            "active": active,
            "idle": idle,
            "idle_in_transaction": idle_tx,
            "waiting": waiting,
            "longest": longest,
            "long_queries": long_queries[:5],
            "restricted": hidden > 0,
        })
        return summary, by_port

    def database_size(self):
        rows = self._fetch(SIZE_SQL)
        if not rows:
            return None
        try:
            return int(rows[0][0])
        except (TypeError, ValueError):                 # pragma: no cover
            return None

    def relation_sizes(self, limit=6):
        rows = self._fetch(RELATION_SQL, (limit,))
        if not rows:
            return []
        sizes = []
        for name, total, indexes, heap in rows:
            sizes.append({"name": name, "total": int(total),
                          "indexes": int(indexes), "heap": int(heap)})
        return sizes
