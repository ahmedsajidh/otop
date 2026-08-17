"""Server-level metrics: CPU, memory, swap, load, disk I/O, network.

psutil does the portable counter reading; the rates are computed here from the
deltas between two samples so that they always match otop's own refresh
interval.  Every collector degrades to ``None`` (rendered as ``N/A``) instead of
raising.
"""

from __future__ import annotations

import os
import time

try:
    import psutil
except ImportError:                                     # pragma: no cover
    psutil = None


class Rate:
    """Per-second rate of a monotonically increasing counter."""

    def __init__(self):
        self.previous = None
        self.timestamp = None

    def update(self, value, now):
        rate = None
        if value is None:
            return None
        if self.previous is not None and self.timestamp is not None:
            elapsed = now - self.timestamp
            delta = value - self.previous
            if elapsed > 0 and delta >= 0:
                rate = delta / elapsed
        self.previous, self.timestamp = value, now
        return rate


class SystemSampler:
    def __init__(self):
        self._read = Rate()
        self._write = Rate()
        self._read_ops = Rate()
        self._write_ops = Rate()
        self._rx = Rate()
        self._tx = Rate()
        if psutil is not None:                          # prime the percentages
            try:
                psutil.cpu_percent(percpu=True)
                psutil.cpu_times_percent()
            except Exception:                           # pragma: no cover
                pass

    # -- pieces ---------------------------------------------------------
    def cpu(self):
        data = {"percent": None, "per_core": [], "cores": None, "load": None,
                "iowait": None}
        if psutil is not None:
            try:
                data["per_core"] = [round(value, 1)
                                    for value in psutil.cpu_percent(percpu=True)]
                data["cores"] = len(data["per_core"])
                if data["per_core"]:
                    data["percent"] = round(
                        sum(data["per_core"]) / len(data["per_core"]), 1)
            except Exception:
                pass
            try:
                data["iowait"] = round(
                    getattr(psutil.cpu_times_percent(), "iowait", 0.0), 1)
            except Exception:
                pass
        if data["cores"] is None:
            data["cores"] = os.cpu_count()
        try:
            data["load"] = [round(value, 2) for value in os.getloadavg()]
        except (OSError, AttributeError):
            pass
        return data

    def memory(self):
        data = {"total": None, "used": None, "available": None, "percent": None,
                "swap_total": None, "swap_used": None, "swap_percent": None}
        if psutil is None:
            return data
        try:
            virtual = psutil.virtual_memory()
            data["total"] = virtual.total
            data["available"] = virtual.available
            data["used"] = virtual.total - virtual.available
            data["percent"] = round(virtual.percent, 1)
        except Exception:
            pass
        try:
            swap = psutil.swap_memory()
            data["swap_total"] = swap.total
            data["swap_used"] = swap.used
            data["swap_percent"] = round(swap.percent, 1)
        except Exception:
            pass
        return data

    def disk_io(self, now):
        data = {"read": None, "write": None, "read_ops": None, "write_ops": None}
        if psutil is None:
            return data
        try:
            counters = psutil.disk_io_counters()
        except Exception:
            counters = None
        if counters:
            data["read"] = self._read.update(counters.read_bytes, now)
            data["write"] = self._write.update(counters.write_bytes, now)
            data["read_ops"] = self._read_ops.update(counters.read_count, now)
            data["write_ops"] = self._write_ops.update(counters.write_count, now)
        return data

    def network(self, now):
        data = {"rx": None, "tx": None}
        if psutil is None:
            return data
        try:
            counters = psutil.net_io_counters()
        except Exception:
            counters = None
        if counters:
            data["rx"] = self._rx.update(counters.bytes_recv, now)
            data["tx"] = self._tx.update(counters.bytes_sent, now)
        return data

    def host(self):
        data = {"hostname": None, "uptime": None}
        try:
            data["hostname"] = os.uname().nodename
        except Exception:                               # pragma: no cover
            pass
        try:
            with open("/proc/uptime") as handle:
                data["uptime"] = float(handle.read().split()[0])
        except (OSError, ValueError, IndexError):
            pass
        return data

    # -- everything -----------------------------------------------------
    def sample(self, now=None):
        now = now or time.time()
        return {
            "time": now,
            "host": self.host(),
            "cpu": self.cpu(),
            "memory": self.memory(),
            "disk_io": self.disk_io(now),
            "network": self.network(now),
        }
