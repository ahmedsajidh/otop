import os
import tempfile
import unittest

from otop import config as config_module

ODOO_CONF = """\
[options]
admin_passwd = secret
data_dir = /var/lib/odoo
db_host = 10.0.0.5
db_port = 5433
db_user = odoo
db_password = hunter2
db_name = odoo_live
http_port = 8070
workers = 6
max_cron_threads = 2
"""


class OdooConfTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".conf")
        with os.fdopen(handle, "w") as file_:
            file_.write(ODOO_CONF)
        self.addCleanup(os.unlink, self.path)

    def test_values_are_derived_from_odoo_conf(self):
        cfg = config_module.parse({"instances": {
            "live": {"name": "LIVE", "odoo_conf": self.path}}})
        instance = cfg.instance("live")
        self.assertEqual(instance.database, "odoo_live")
        self.assertEqual(instance.filestore, "/var/lib/odoo/filestore/odoo_live")
        self.assertEqual(instance.http_port, 8070)
        self.assertEqual(instance.workers, 6)
        self.assertEqual(instance.max_cron_threads, 2)
        self.assertEqual(instance.db_host, "10.0.0.5")
        self.assertEqual(instance.db_port, 5433)
        self.assertEqual(instance.process_match, self.path)

    def test_explicit_values_win_over_odoo_conf(self):
        cfg = config_module.parse({"instances": {"live": {
            "odoo_conf": self.path,
            "database": "other_db",
            "http_port": 9000,
            "filestore": "/srv/filestore",
            "db": {"host": "localhost", "user": "monitor", "password": "x"},
        }}})
        instance = cfg.instance("live")
        self.assertEqual(instance.database, "other_db")
        self.assertEqual(instance.http_port, 9000)
        self.assertEqual(instance.filestore, "/srv/filestore")
        self.assertEqual(instance.db_user, "monitor")

    def test_unreadable_odoo_conf_is_a_warning_not_a_crash(self):
        cfg = config_module.parse({"instances": {
            "live": {"odoo_conf": "/nope/does-not-exist.conf"}}})
        instance = cfg.instance("live")
        self.assertTrue(instance.warnings)
        self.assertIsNone(instance.database)
        self.assertEqual(instance.http_port, 8069)      # falls back to the default

    def test_connect_kwargs_carry_credentials_and_timeouts(self):
        cfg = config_module.parse({"instances": {
            "live": {"odoo_conf": self.path}}})
        kwargs = cfg.instance("live").connect_kwargs()
        self.assertEqual(kwargs["dbname"], "odoo_live")
        self.assertEqual(kwargs["password"], "hunter2")
        self.assertEqual(kwargs["connect_timeout"], 3)
        self.assertIn("statement_timeout", kwargs["options"])

    def test_no_database_means_no_connection(self):
        cfg = config_module.parse({"instances": {"live": {"process_match": "odoo"}}})
        self.assertIsNone(cfg.instance("live").connect_kwargs())


class YamlDocumentTest(unittest.TestCase):
    def test_defaults(self):
        cfg = config_module.parse({"instances": {"live": {"database": "db"}}})
        self.assertEqual(cfg.refresh, 2.0)
        self.assertEqual(cfg.filestore_refresh, 300.0)
        self.assertEqual(cfg.disk_path, "/")
        self.assertTrue(cfg.show_query_text)

    def test_refresh_mapping_and_scalar(self):
        cfg = config_module.parse({
            "refresh": {"fast": 5, "slow": 60, "filestore": 900},
            "instances": {"live": {"database": "db"}}})
        self.assertEqual(cfg.refresh, 5.0)
        self.assertEqual(cfg.slow_refresh, 60.0)
        self.assertEqual(cfg.filestore_refresh, 900.0)

        cfg = config_module.parse({"refresh": 3, "instances": {"a": {"database": "d"}}})
        self.assertEqual(cfg.refresh, 3.0)

    def test_instance_order_is_preserved_and_disabled_skipped(self):
        cfg = config_module.parse({"instances": {
            "live": {"name": "LIVE", "database": "a"},
            "staging": {"name": "STAGING", "database": "b"},
            "old": {"database": "c", "enabled": False},
        }})
        self.assertEqual([i.key for i in cfg.instances], ["live", "staging"])

    def test_list_form_is_accepted(self):
        cfg = config_module.parse({"instances": [
            {"key": "live", "name": "LIVE", "database": "a"}]})
        self.assertEqual(cfg.instances[0].name, "LIVE")

    def test_no_instances_is_an_error(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.parse({"instances": {}})

    def test_non_mapping_document_is_an_error(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.parse(["not", "a", "mapping"])

    def test_instance_without_match_warns(self):
        cfg = config_module.parse({"instances": {"live": {"database": "db"}}})
        self.assertTrue(cfg.instance("live").warnings)


if __name__ == "__main__":
    unittest.main()
