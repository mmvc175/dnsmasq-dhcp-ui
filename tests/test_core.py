"""核心逻辑测试：配置校验、配置渲染、租约解析、剩余租期、在线判定。

运行： python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config as config_mod
from app import dnsmasq_conf, leases
from app.activity import ActivityStore
from app.config import ValidationError, normalize_mac


def sample_config():
    return {
        "version": 1,
        "dhcp": {
            "enabled": True,
            "interface": "eth0",
            "range_start": "192.168.1.100",
            "range_end": "192.168.1.200",
            "netmask": "255.255.255.0",
            "lease_time": "24h",
            "gateway": "192.168.1.1",
            "dns_servers": ["192.168.1.1", "223.5.5.5"],
            "domain": "lan",
            "authoritative": True,
        },
        "dns": {
            "enabled": True,
            "forwarders": ["223.5.5.5"],
            "no_resolv": True,
        },
        "static_leases": [
            {
                "name": "nas",
                "mac": "AA:BB:CC:DD:EE:FF",
                "ip": "192.168.1.10",
                "gateway": "192.168.1.254",
                "dns_servers": ["8.8.8.8", "1.1.1.1"],
                "lease_time": "",
                "enabled": True,
                "note": "存储服务器",
            }
        ],
        "ui": {"online_window": 900},
        "auth": {"enabled": False, "username": "admin", "password": ""},
    }


class TestValidation(unittest.TestCase):
    def test_mac_normalize(self):
        self.assertEqual(normalize_mac("aa-bb-cc-dd-ee-ff"), "AA:BB:CC:DD:EE:FF")
        self.assertEqual(normalize_mac("aabb.ccdd.eeff"), "AA:BB:CC:DD:EE:FF")

    def test_static_entry_ok(self):
        entry = config_mod.validate_static_entry(
            {"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.20", "name": "printer-01",
             "dns_servers": "8.8.8.8,1.1.1.1"}
        )
        self.assertEqual(entry["mac"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(entry["dns_servers"], ["8.8.8.8", "1.1.1.1"])

    def test_static_entry_bad_mac(self):
        with self.assertRaises(ValidationError):
            config_mod.validate_static_entry({"mac": "xyz", "ip": "192.168.1.20"})

    def test_static_entry_bad_ip(self):
        with self.assertRaises(ValidationError):
            config_mod.validate_static_entry({"mac": "AA:BB:CC:DD:EE:FF", "ip": "300.1.1.1"})

    def test_dhcp_range_order(self):
        with self.assertRaises(ValidationError):
            config_mod.validate_dhcp_block(
                {"range_start": "192.168.1.200", "range_end": "192.168.1.100"}
            )

    def test_lease_time_formats(self):
        for value in ("24h", "30m", "7d", "infinite", "3600"):
            self.assertTrue(config_mod.is_valid_lease_time(value), value)
        for value in ("abc", "24x", ""):
            self.assertFalse(config_mod.is_valid_lease_time(value), value)

    def test_netmask_cidr(self):
        self.assertEqual(config_mod.normalize_netmask("/24"), "255.255.255.0")
        self.assertEqual(config_mod.normalize_netmask("24"), "255.255.255.0")
        self.assertEqual(config_mod.normalize_netmask("255.255.0.0"), "255.255.0.0")
        with self.assertRaises(ValidationError):
            config_mod.normalize_netmask("999.0.0.0")


class TestRender(unittest.TestCase):
    def test_render_contains_range_and_options(self):
        text = dnsmasq_conf.render_managed(sample_config())
        self.assertIn("dhcp-range=192.168.1.100,192.168.1.200,255.255.255.0,24h", text)
        self.assertIn("dhcp-option=3,192.168.1.1", text)
        self.assertIn("dhcp-option=6,192.168.1.1,223.5.5.5", text)
        self.assertIn("interface=eth0", text)
        self.assertIn("domain=lan", text)
        self.assertIn("dhcp-authoritative", text)

    def test_render_static_with_tag(self):
        text = dnsmasq_conf.render_managed(sample_config())
        self.assertIn("dhcp-host=AA:BB:CC:DD:EE:FF,set:host_aabbccddeeff,192.168.1.10,nas", text)
        self.assertIn("dhcp-option=tag:host_aabbccddeeff,3,192.168.1.254", text)
        self.assertIn("dhcp-option=tag:host_aabbccddeeff,6,8.8.8.8,1.1.1.1", text)

    def test_render_dhcp_disabled(self):
        cfg = sample_config()
        cfg["dhcp"]["enabled"] = False
        text = dnsmasq_conf.render_managed(cfg)
        self.assertNotIn("dhcp-range=", text)
        # 静态绑定仍然渲染，便于后续重新开启
        self.assertIn("dhcp-host=", text)

    def test_render_dns_disabled(self):
        cfg = sample_config()
        cfg["dns"]["enabled"] = False
        text = dnsmasq_conf.render_managed(cfg)
        self.assertNotIn("server=223.5.5.5", text)

    def test_render_hosts(self):
        text = dnsmasq_conf.render_hosts(sample_config())
        self.assertIn("192.168.1.10\tnas", text)
        self.assertIn("192.168.1.10\tnas.lan", text)

    def test_disabled_static_is_commented(self):
        cfg = sample_config()
        cfg["static_leases"][0]["enabled"] = False
        text = dnsmasq_conf.render_managed(cfg)
        self.assertNotIn("dhcp-host=AA:BB:CC:DD:EE:FF", text)
        self.assertIn("已停用", text)

    def test_main_conf_port0(self):
        original = dnsmasq_conf.MAIN_CONF
        with tempfile.TemporaryDirectory() as tmp:
            dnsmasq_conf.MAIN_CONF = os.path.join(tmp, "dnsmasq.conf")
            try:
                dnsmasq_conf.write_main_conf("/data/x.leases", "/data/hosts", "/app/scripts/x.py", False)
                with open(dnsmasq_conf.MAIN_CONF, encoding="utf-8") as handle:
                    content = handle.read()
            finally:
                dnsmasq_conf.MAIN_CONF = original
        self.assertIn("port=0", content)
        self.assertNotIn("port=53", content)
        self.assertIn("dhcp-leasefile=/data/x.leases", content)


class TestLeases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".leases", delete=False)
        self.now = int(time.time())
        self.tmp.write(
            "\n".join(
                [
                    "%d aa:bb:cc:dd:ee:ff 192.168.1.10 nas 01:aa:bb:cc:dd:ee:ff" % (self.now + 3600),
                    "%d 11:22:33:44:55:66 192.168.1.101 laptop *" % (self.now + 7200),
                    "0 99:88:77:66:55:44 192.168.1.30 camera *",
                    "%d dd:ee:ff:00:11:22 192.168.1.150 old *" % (self.now - 60),
                    "",
                ]
            )
        )
        self.tmp.close()
        self.statics = [
            {"name": "nas", "mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.1.10", "enabled": True},
            {"name": "camera", "mac": "99:88:77:66:55:44", "ip": "192.168.1.30", "enabled": True},
            {"name": "offline-nas", "mac": "DE:AD:BE:EF:00:01", "ip": "192.168.1.60", "enabled": True},
        ]

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_parse_and_remaining(self):
        rows = leases.build_leases(self.tmp.name, self.statics, {}, online_window=900)
        by_ip = {row["ip"]: row for row in rows}

        self.assertEqual(by_ip["192.168.1.10"]["hostname"], "nas")
        self.assertEqual(by_ip["192.168.1.10"]["type"], "static")
        self.assertTrue(by_ip["192.168.1.10"]["isStatic"])
        self.assertFalse(by_ip["192.168.1.10"]["remaining"]["expired"])
        # 1 小时租约，扣除测试耗时应显示 "59分xx秒" 或 "1小时0分"
        self.assertRegex(by_ip["192.168.1.10"]["remaining"]["text"], r"小时|分")

        self.assertEqual(by_ip["192.168.1.101"]["type"], "dynamic")
        self.assertEqual(by_ip["192.168.1.101"]["hostname"], "laptop")

        # 永久租约
        self.assertTrue(by_ip["192.168.1.30"]["remaining"]["permanent"])
        self.assertEqual(by_ip["192.168.1.30"]["remaining"]["text"], "永久")

        # 已过期
        self.assertTrue(by_ip["192.168.1.150"]["remaining"]["expired"])
        self.assertEqual(by_ip["192.168.1.150"]["remaining"]["text"], "已过期")

    def test_static_without_lease_is_listed(self):
        rows = leases.build_leases(self.tmp.name, self.statics, {}, online_window=900)
        offline = [r for r in rows if r["mac"] == "DE:AD:BE:EF:00:01"]
        self.assertEqual(len(offline), 1)
        self.assertEqual(offline[0]["remaining"]["text"], "未分配")
        self.assertFalse(offline[0]["online"])

    def test_online_by_activity(self):
        activity = {
            "AA:BB:CC:DD:EE:FF": self.now - 30,
            "11:22:33:44:55:66": self.now - 7200,
        }
        rows = leases.build_leases(self.tmp.name, self.statics, activity, online_window=900)
        by_mac = {row["mac"]: row for row in rows}
        self.assertTrue(by_mac["AA:BB:CC:DD:EE:FF"]["online"])
        self.assertFalse(by_mac["11:22:33:44:55:66"]["online"])
        self.assertEqual(by_mac["AA:BB:CC:DD:EE:FF"]["lastSeenText"], "刚刚")

    def test_sorted_by_ip(self):
        rows = leases.build_leases(self.tmp.name, self.statics, {}, online_window=900)
        ips = [row["ip"] for row in rows]
        self.assertEqual(ips, sorted(ips, key=leases.ip_sort_key))
        self.assertEqual(ips[0], "192.168.1.10")

    def test_summary(self):
        rows = leases.build_leases(self.tmp.name, self.statics, {}, online_window=900)
        summary = leases.summarize(rows)
        self.assertEqual(summary["total"], len(rows))
        self.assertEqual(summary["static"], 3)
        self.assertEqual(summary["dynamic"], 2)

    def test_missing_file(self):
        rows = leases.build_leases("/nonexistent/leases.file", [], {}, online_window=900)
        self.assertEqual(rows, [])


class TestActivity(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "activity.json")
            store = ActivityStore(path)
            store.load()
            store.touch("aa:bb:cc:dd:ee:ff")
            store.flush()

            other = ActivityStore(path)
            other.load()
            self.assertIn("AA:BB:CC:DD:EE:FF", other.snapshot())

            other.forget("AA:BB:CC:DD:EE:FF")
            other.flush()
            third = ActivityStore(path)
            third.load()
            self.assertEqual(third.snapshot(), {})

    def test_merge_with_external_writer(self):
        """dnsmasq 回调脚本在外部进程写入后，主进程快照必须可见。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "activity.json")
            now = int(time.time())

            main = ActivityStore(path)
            main.load()
            main.touch("AA:BB:CC:DD:EE:FF", now - 600)
            main.flush()

            external = ActivityStore(path)      # 模拟 lease_notify.py 进程
            external.load()
            external.touch("AA:BB:CC:DD:EE:FF", now - 10)
            external.touch("11:22:33:44:55:66", now - 5)
            external.flush()

            snap = main.snapshot()
            self.assertEqual(snap["AA:BB:CC:DD:EE:FF"], now - 10)
            self.assertEqual(snap["11:22:33:44:55:66"], now - 5)

    def test_flush_preserves_newer_external_value(self):
        """并发写回时不能把其它进程写入的更晚时间戳覆盖掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "activity.json")
            now = int(time.time())

            a = ActivityStore(path)
            a.load()
            a.touch("AA:BB:CC:DD:EE:FF", now - 600)
            a.flush()

            b = ActivityStore(path)
            b.load()
            b.touch("AA:BB:CC:DD:EE:FF", now - 20)
            b.flush()

            a.touch("11:22:33:44:55:66", now - 30)
            a.flush()

            final = ActivityStore(path)
            final.load()
            self.assertEqual(final.snapshot()["AA:BB:CC:DD:EE:FF"], now - 20)
            self.assertEqual(final.snapshot()["11:22:33:44:55:66"], now - 30)

    def test_expired_records_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "activity.json")
            store = ActivityStore(path)
            store.load()
            store.touch("AA:BB:CC:DD:EE:FF", int(time.time()) - 40 * 86400)
            store.prune()
            store.flush()
            reloaded = ActivityStore(path)
            reloaded.load()
            self.assertEqual(reloaded.snapshot(), {})


class TestConfigPersistence(unittest.TestCase):
    def test_env_defaults_and_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DHCP_RANGE_START"] = "10.0.0.50"
            os.environ["DHCP_RANGE_END"] = "10.0.0.150"
            try:
                cfg = config_mod.AppConfig(os.path.join(tmp, "config.json"))
                cfg.load()
                self.assertEqual(cfg.snapshot()["dhcp"]["range_start"], "10.0.0.50")
                cfg.save()

                reloaded = config_mod.AppConfig(os.path.join(tmp, "config.json"))
                reloaded.load()
                self.assertEqual(reloaded.snapshot()["dhcp"]["range_end"], "10.0.0.150")
            finally:
                os.environ.pop("DHCP_RANGE_START", None)
                os.environ.pop("DHCP_RANGE_END", None)

    def test_stored_config_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"dhcp":{"range_start":"172.16.5.10"}}')
            cfg = config_mod.AppConfig(path)
            cfg.load()
            data = cfg.snapshot()
            self.assertEqual(data["dhcp"]["range_start"], "172.16.5.10")
            # 缺失字段由默认值补齐
            self.assertEqual(data["dhcp"]["lease_time"], "24h")

    def test_auth_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["WEB_USER"] = "admin"
            os.environ["WEB_PASS"] = "s3cret"
            try:
                cfg = config_mod.AppConfig(os.path.join(tmp, "config.json"))
                cfg.load()
                auth = cfg.snapshot()["auth"]
                self.assertTrue(auth["enabled"])
                self.assertEqual(auth["username"], "admin")
                self.assertEqual(auth["password"], "s3cret")
            finally:
                os.environ.pop("WEB_USER", None)
                os.environ.pop("WEB_PASS", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
