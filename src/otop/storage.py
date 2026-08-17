"""Storage: total disk first, then database / filestore / other.

The filestore size is the real size of the directory, never inferred from the
database.  Walking a filestore with millions of files is far too expensive to do
on every refresh, so each filestore is walked at most every
``filestore_refresh`` seconds in a background thread; the UI shows how old the
figure is and never blocks on it.
"""

from __future__ import annotations

import os
import threading
import time


class DirectorySize:
    """Cached, background-refreshed size of one directory."""

    def __init__(self, path, interval=300.0):
        self.path = path
        self.interval = interval
        self.bytes = None
        self.files = None
        self.updated = None
        self.duration = None
        self.error = None
        self.scanning = False
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            return {
                "path": self.path,
                "bytes": self.bytes,
                "files": self.files,
                "age": (time.time() - self.updated) if self.updated else None,
                "duration": self.duration,
                "scanning": self.scanning,
                "error": self.error,
            }

    def refresh(self, force=False):
        """Start a walk if the cached value is old enough (non-blocking)."""
        with self._lock:
            if self.scanning:
                return
            if not force and self.updated and (time.time() - self.updated) < self.interval:
                return
            self.scanning = True
        thread = threading.Thread(target=self._walk, name="otop-filestore",
                                  daemon=True)
        thread.start()

    def _walk(self):
        started = time.time()
        total = 0
        count = 0
        error = None
        if not self.path:
            error = "no filestore path configured"
            total = count = None
        elif not os.path.isdir(self.path):
            error = "not found: %s" % self.path
            total = count = None
        else:
            stack = [self.path]
            while stack:
                current = stack.pop()
                try:
                    with os.scandir(current) as entries:
                        for entry in entries:
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    stack.append(entry.path)
                                elif entry.is_file(follow_symlinks=False):
                                    total += entry.stat(follow_symlinks=False).st_size
                                    count += 1
                            except OSError:
                                continue
                except PermissionError:
                    error = "partial: permission denied"
                except OSError as exc:
                    error = exc.strerror or str(exc)
        with self._lock:
            self.bytes = total
            self.files = count
            self.error = error
            self.duration = round(time.time() - started, 2)
            self.updated = time.time()
            self.scanning = False


def disk_usage(path):
    """Total/used/free of the filesystem holding *path* (statvfs, no psutil)."""
    data = {"path": path, "total": None, "used": None, "free": None,
            "percent": None, "device": None, "error": None}
    try:
        stat = os.statvfs(path)
        data["device"] = os.stat(path).st_dev
    except OSError as exc:
        data["error"] = exc.strerror or str(exc)
        return data
    block = stat.f_frsize or stat.f_bsize
    total = stat.f_blocks * block
    free = stat.f_bavail * block                        # available to non-root
    used = (stat.f_blocks - stat.f_bfree) * block
    data["total"] = total
    data["free"] = free
    data["used"] = used
    denominator = used + free
    if denominator:
        data["percent"] = round(100.0 * used / denominator, 1)
    return data


def same_filesystem(path, device):
    if not path or device is None:
        return True
    try:
        return os.stat(path).st_dev == device
    except OSError:
        return True


def compose(disk, entries, pg_data_dir=""):
    """Build the breakdown rows and the 'other' remainder.

    ``entries`` is a list of dicts: {label, kind, bytes, path, age, error}.
    Anything that lives on a different filesystem than the monitored disk is
    still shown, but excluded from the subtraction (and flagged), otherwise
    'other' would be wrong.
    """
    device = disk.get("device")
    rows = []
    accounted = 0
    for entry in entries:
        row = dict(entry)
        if row.get("kind") == "database" and pg_data_dir:
            row["same_fs"] = same_filesystem(pg_data_dir, device)
        elif row.get("path"):
            row["same_fs"] = same_filesystem(row["path"], device)
        else:
            row["same_fs"] = True
        if row.get("bytes") and row["same_fs"]:
            accounted += row["bytes"]
        rows.append(row)
    other = None
    if disk.get("used") is not None:
        other = max(0, disk["used"] - accounted)
    return {"rows": rows, "other": other, "accounted": accounted}
