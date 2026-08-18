"""Configuration loading for otop.

The configuration lives outside the application source, by default in
``/etc/otop/config.yaml``.  Search order:

    1. ``--config PATH``
    2. ``$OTOP_CONFIG``
    3. ``~/.config/otop/config.yaml``
    4. ``/etc/otop/config.yaml``

Each instance may simply point at the Odoo instance's own ``odoo.conf``; the
database name, filestore path, HTTP port, worker counts and database
credentials are then read from there, so nothing has to be duplicated (and no
paths or credentials are ever hard-coded in otop itself).
"""

from __future__ import annotations

import configparser
import os

try:
    import yaml
except ImportError:                                     # pragma: no cover
    yaml = None

CONFIG_ENV = "OTOP_CONFIG"
SYSTEM_CONFIG = "/etc/otop/config.yaml"
USER_CONFIG = os.path.expanduser("~/.config/otop/config.yaml")

DEFAULTS = {
    "refresh": 2.0,             # seconds between cheap samples
    "slow_refresh": 30.0,       # database size
    "filestore_refresh": 300.0, # filestore directory walk
    "table_refresh": 900.0,     # per-table / per-index sizes
    "discovery_refresh": 10.0,  # full process table rescan
    "disk_path": "/",
    "pg_data_dir": "",
    "long_query_seconds": 5.0,
    "busy_cpu_threshold": 20.0,
    "show_query_text": True,
    "routes": True,             # tail the Odoo access log for per-route timings
    "routes_window": 900.0,     # rolling window for the route statistics
    "routes_max_events": 20000, # hard cap on remembered requests, per instance
}

FALSY_ODOO = {"", "false", "none", "0"}


class ConfigError(Exception):
    """Raised only for a configuration that cannot be used at all."""


class Instance:
    """One monitored Odoo instance."""

    __slots__ = (
        "database",
        "db_host",
        "db_password",
        "db_port",
        "db_user",
        "filestore",
        "http_port",
        "key",
        "logfile",
        "max_cron_threads",
        "name",
        "odoo_conf",
        "process_match",
        "warnings",
        "workers",
    )

    def __init__(self, key, name=None):
        self.key = key
        self.name = name or key.upper()
        self.odoo_conf = None
        self.process_match = None
        self.database = None
        self.filestore = None
        self.http_port = None
        self.logfile = None
        self.workers = None
        self.max_cron_threads = None
        self.db_host = None
        self.db_port = 5432
        self.db_user = None
        self.db_password = None
        self.warnings = []

    def connect_kwargs(self):
        """psycopg connection arguments.  Never displayed, never logged."""
        if not self.database:
            return None
        kwargs = {
            "dbname": self.database,
            "connect_timeout": 3,
            "application_name": "otop",
            "options": "-c statement_timeout=5000",
        }
        if self.db_host:
            kwargs["host"] = self.db_host
        if self.db_port:
            kwargs["port"] = self.db_port
        if self.db_user:
            kwargs["user"] = self.db_user
        if self.db_password:
            kwargs["password"] = self.db_password
        return kwargs

    def __repr__(self):                                 # pragma: no cover
        return "<Instance %s db=%s port=%s>" % (self.key, self.database, self.http_port)


class Config:
    def __init__(self):
        self.path = None
        self.instances = []
        self.warnings = []
        for key, value in DEFAULTS.items():
            setattr(self, key, value)

    def instance(self, key):
        for inst in self.instances:
            if inst.key == key:
                return inst
        return None


# ---------------------------------------------------------------------------
# odoo.conf
# ---------------------------------------------------------------------------
def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return None if value.lower() in FALSY_ODOO else value


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def read_odoo_conf(path):
    """Return the ``[options]`` section of an odoo.conf as a plain dict."""
    parser = configparser.RawConfigParser()
    with open(path) as handle:
        parser.read_file(handle)
    if not parser.has_section("options"):
        raise ConfigError("%s has no [options] section" % path)
    return dict(parser.items("options"))


def apply_odoo_conf(inst):
    """Fill in everything the instance did not set explicitly."""
    try:
        options = read_odoo_conf(inst.odoo_conf)
    except (OSError, configparser.Error, ConfigError) as exc:
        inst.warnings.append("cannot read %s: %s" % (inst.odoo_conf, exc))
        return

    if inst.database is None:
        db_name = _clean(options.get("db_name"))
        if db_name:
            # db_name may be a comma separated list; one instance = one database
            inst.database = db_name.split(",")[0].strip()
    if inst.db_host is None:
        inst.db_host = _clean(options.get("db_host"))
    if inst.db_user is None:
        inst.db_user = _clean(options.get("db_user"))
    if inst.db_password is None:
        inst.db_password = _clean(options.get("db_password"))
    if inst.db_port == 5432:
        inst.db_port = _int(options.get("db_port"), 5432)
    if inst.http_port is None:
        inst.http_port = _int(options.get("http_port"), 8069)
    # Both keys are optional in odoo.conf; when they are absent Odoo uses these
    # defaults, and so must we -- otherwise a perfectly normal odoo.conf that
    # only sets `workers` leaves the worker counts unknown and every child
    # process ends up untyped.
    if inst.workers is None:
        inst.workers = _int(options.get("workers"), 0)
    if inst.max_cron_threads is None:
        inst.max_cron_threads = _int(options.get("max_cron_threads"), 2)
    if inst.logfile is None:
        logfile = _clean(options.get("logfile"))
        if logfile:
            # A relative logfile is relative to Odoo's working directory, which
            # the file alone does not reveal; the instance directory is the
            # usual answer, and the running command line settles it later on.
            inst.logfile = logfile if os.path.isabs(logfile) else os.path.normpath(
                os.path.join(os.path.dirname(os.path.abspath(inst.odoo_conf)), logfile))
    if inst.filestore is None and inst.database:
        data_dir = _clean(options.get("data_dir")) or os.path.expanduser(
            "~/.local/share/Odoo")
        inst.filestore = os.path.join(data_dir, "filestore", inst.database)


# ---------------------------------------------------------------------------
# yaml
# ---------------------------------------------------------------------------
def find_config(explicit=None):
    """First existing configuration file, or None."""
    for candidate in (explicit, os.environ.get(CONFIG_ENV), USER_CONFIG, SYSTEM_CONFIG):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def parse(document, path=None):
    """Build a Config from an already parsed YAML mapping."""
    cfg = Config()
    cfg.path = path
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise ConfigError("configuration must be a YAML mapping")

    refresh = document.get("refresh")
    if isinstance(refresh, dict):                       # refresh: {fast: 2, slow: 30}
        mapping = {"fast": "refresh", "slow": "slow_refresh",
                   "filestore": "filestore_refresh", "tables": "table_refresh",
                   "discovery": "discovery_refresh"}
        for key, attr in mapping.items():
            if key in refresh:
                setattr(cfg, attr, _float(refresh[key], getattr(cfg, attr)))
    elif refresh is not None:                           # refresh: 2
        cfg.refresh = _float(refresh, cfg.refresh)

    for key in ("slow_refresh", "filestore_refresh", "table_refresh",
                "discovery_refresh", "long_query_seconds", "busy_cpu_threshold",
                "routes_window"):
        if key in document:
            setattr(cfg, key, _float(document[key], getattr(cfg, key)))
    if "routes_max_events" in document:
        cfg.routes_max_events = _int(document["routes_max_events"],
                                     cfg.routes_max_events)
    for key in ("disk_path", "pg_data_dir"):
        if document.get(key):
            setattr(cfg, key, str(document[key]))
    if "show_query_text" in document:
        cfg.show_query_text = bool(document["show_query_text"])
    if "routes" in document:
        cfg.routes = bool(document["routes"])

    instances = document.get("instances")
    if isinstance(instances, dict):
        items = list(instances.items())
    elif isinstance(instances, list):                   # also accept a list of dicts
        items = [(str(entry.get("key") or entry.get("name") or index), entry)
                 for index, entry in enumerate(instances) if isinstance(entry, dict)]
    else:
        items = []

    for key, values in items:
        if not isinstance(values, dict):
            cfg.warnings.append("instance %r ignored: not a mapping" % key)
            continue
        if values.get("enabled") is False:
            continue
        inst = Instance(str(key), values.get("name"))
        inst.odoo_conf = values.get("odoo_conf") or None
        inst.process_match = values.get("process_match") or None
        inst.database = values.get("database") or None
        inst.filestore = values.get("filestore") or None
        inst.logfile = values.get("logfile") or None
        inst.http_port = _int(values.get("http_port"), None)
        inst.workers = _int(values.get("workers"), None)
        inst.max_cron_threads = _int(values.get("max_cron_threads"), None)

        database = values.get("db")
        if isinstance(database, dict):
            inst.db_host = database.get("host") or None
            inst.db_port = _int(database.get("port"), 5432)
            inst.db_user = database.get("user") or None
            inst.db_password = database.get("password") or None

        if inst.odoo_conf:
            apply_odoo_conf(inst)
        if not inst.process_match:
            inst.process_match = inst.odoo_conf
        if not inst.process_match:
            inst.warnings.append(
                "no odoo_conf and no process_match: worker detection disabled")
        if inst.http_port is None:
            inst.http_port = 8069
        cfg.instances.append(inst)

    if not cfg.instances:
        raise ConfigError("no instances defined")
    return cfg


def load(path=None):
    """Load the configuration file, or raise ConfigError."""
    found = find_config(path)
    if not found:
        raise ConfigError(
            "no configuration file found (looked for %s, $%s, %s, %s)"
            % (path or "--config", CONFIG_ENV, USER_CONFIG, SYSTEM_CONFIG))
    if yaml is None:
        raise ConfigError("PyYAML is not installed (apt install python3-yaml)")
    try:
        with open(found) as handle:
            document = yaml.safe_load(handle)
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (found, exc)) from exc
    except yaml.YAMLError as exc:
        raise ConfigError("invalid YAML in %s: %s" % (found, exc)) from exc
    return parse(document, found)


def _float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
