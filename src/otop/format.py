"""Small formatting helpers shared by the UI and the text dump."""

from __future__ import annotations

NA = "N/A"

UNITS = ("B", "K", "M", "G", "T", "P")


def human_bytes(value, na=NA):
    if value is None:
        return na
    try:
        value = float(value)
    except (TypeError, ValueError):
        return na
    index = 0
    while value >= 1024 and index < len(UNITS) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return "%d B" % value
    if value >= 100:
        return "%.0f %sB" % (value, UNITS[index])
    return "%.1f %sB" % (value, UNITS[index])


def human_rate(value, na=NA):
    if value is None:
        return na
    return human_bytes(value, na) + "/s"


def human_percent(value, na=NA, digits=0):
    if value is None:
        return na
    return "%.*f%%" % (digits, value)


def human_seconds(value, na=NA):
    if value is None:
        return na
    value = float(value)
    if value < 60:
        return "%.0fs" % value if value >= 10 else "%.1fs" % value
    if value < 3600:
        return "%dm%02ds" % (value // 60, value % 60)
    if value < 86400:
        return "%dh%02dm" % (value // 3600, (value % 3600) // 60)
    return "%dd%02dh" % (value // 86400, (value % 86400) // 3600)


def human_duration(value, na=NA):
    """Like human_seconds, but readable below a second: 34ms, 1.20s, 59.2s."""
    if value is None:
        return na
    try:
        value = float(value)
    except (TypeError, ValueError):
        return na
    if value < 0:
        return na
    if value < 0.0005:
        return "0ms"
    if value < 1:
        return "%.0fms" % (value * 1000)
    if value < 10:
        return "%.2fs" % value
    if value < 60:
        return "%.1fs" % value
    return human_seconds(value)


def human_count(value, na=NA):
    return na if value is None else str(value)


def bar(percent, width, filled="|", empty=" "):
    """A simple ``[|||||     ]`` gauge, htop style."""
    if width < 3:
        return ""
    inner = width - 2
    if percent is None:
        return "[" + "?".center(inner) + "]"
    ratio = max(0.0, min(100.0, float(percent))) / 100.0
    count = int(round(ratio * inner))
    return "[" + filled * count + empty * (inner - count) + "]"


def level(percent, warn=75.0, crit=90.0):
    """Style token for a percentage."""
    if percent is None:
        return "dim"
    if percent >= crit:
        return "crit"
    if percent >= warn:
        return "warn"
    return "good"


def fit(text, width):
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[:width - 1] + "~"
