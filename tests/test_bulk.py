"""批量导入测试：配置文本解析、渲染、导入接口。"""

import base64
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="dhcpui-bulk-")
os.environ["DATA_DIR"] = TMP
os.environ["HOSTS_FILE"] = os.path.join(TMP, "hosts.dnsmasq")
os.environ["LEASE_SCRIPT"] = os.path.join(ROOT, "scripts", "lease_notify.py")

from app import api as api_mod, bulk, dnsmasq_conf  # noqa: E402
from app.activity import ActivityStore  # noqa: E402
from app.config import AppConfig, ValidationError  # noqa: E402

# 用户实际提供的样例
USER_SAMPLE = """dhcp-host=6E:53:C6:69:A9:DA,set:host_6e53c669a9da,192.168.1.21,fnos,infinite
dhcp-host=42:F2:2C:11:F9:23,set:host_42f22c11f923,192.168.1.22,win10-edge
dhcp-host=B0:5C:DA:6A:AD:78,set:host_b05cda6aad78,192.168.1.31,M427FN
dhcp-host=58:05:D9:3C:1A:F1,set:host_5805d93c1af1,192.168.1.32,EPSON
dhcp-host=34:9F:7B:A2:D8:3E,set:host_349f7ba2d83e,192.168.1.33,MF742
dhcp-host=A0:8C:FD:DF:E0:8D,set:host_a08cfddfe08d,192.168.1.40,pc-lvwei
dhcp-host=00:E2:69:8B:37:52,set:host_00e2698b3752,192.168.1.41,pc-chenqian
dhcp-host=F8:0F:41:53:06:0E,set:host_f80f4153060e,192.168.1.42,pc-liuyingmei"""


class TestParse(unittest.TestCase):
    def test_user_sample(self):
        result = bulk.parse_config_text(USER_SAMPLE)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(len(result.entries), 8)

        first = result.entries[0]
        self.assertEqual(first["mac"], "6E:53:C6:69:A9:DA")
        self.assertEqual(first["ip"], "192.168.1.21")
        self.assertEqual(first["name"], "fnos")
        self.assertEqual(first["lease_time"], "infinite")
        self.assertTrue(first["enabled"])

        second = result.entries[1]
        self.assertEqual(second["name"], "win10-edge")
        self.assertEqual(second["lease_time"], "")

        self.assertEqual(result.entries[5]["name"], "pc-lvwei")
        self.assertEqual(result.entries[7]["ip"], "192.168.1.42")

    def test_round_trip_is_stable(self):
        """解析后重新渲染，应与原文一致（幂等）。"""
        once = bulk.parse_config_text(USER_SAMPLE)
        text = bulk.render_static_text(once.entries, header=False).strip()
        twice = bulk.parse_config_text(text)
        self.assertTrue(twice.ok, twice.errors)
        self.assertEqual(
            [(e["mac"], e["ip"], e["name"], e["lease_time"]) for e in once.entries],
            [(e["mac"], e["ip"], e["name"], e["lease_time"]) for e in twice.entries],
        )

    def test_gateway_and_dns_via_tag(self):
        text = (
            "dhcp-host=B0:5C:DA:6A:AD:78,set:host_b05cda6aad78,192.168.1.31,M427FN\n"
            "dhcp-option=tag:host_b05cda6aad78,3,192.168.1.254\n"
            "dhcp-option=tag:host_b05cda6aad78,6,223.5.5.5,114.114.114.114"
        )
        result = bulk.parse_config_text(text)
        self.assertTrue(result.ok)
        entry = result.entries[0]
        self.assertEqual(entry["gateway"], "192.168.1.254")
        self.assertEqual(entry["dns_servers"], ["223.5.5.5", "114.114.114.114"])

    def test_mac_formats(self):
        for text in (
            "dhcp-host=aa:bb:cc:dd:ee:ff,192.168.1.10,a",
            "dhcp-host=AA-BB-CC-DD-EE-FF,192.168.1.10,a",
            "dhcp-host=aabb.ccdd.eeff,192.168.1.10,a",
        ):
            result = bulk.parse_config_text(text)
            self.assertTrue(result.ok, text)
            self.assertEqual(result.entries[0]["mac"], "AA:BB:CC:DD:EE:FF")

    def test_bare_line_without_prefix(self):
        result = bulk.parse_config_text("AA:BB:CC:00:11:22,192.168.1.62,printer")
        self.assertTrue(result.ok)
        self.assertEqual(result.entries[0]["name"], "printer")

    def test_ignore_marks_disabled(self):
        result = bulk.parse_config_text(
            "dhcp-host=11:22:33:44:55:66,set:host_112233445566,192.168.1.60,old-pc,ignore"
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.entries[0]["enabled"])

    def test_comments_and_blank_lines(self):
        result = bulk.parse_config_text(
            "# 打印机区\n\n"
            "dhcp-host=AA:BB:CC:00:11:24,192.168.1.63,pc1\n"
            "\n"
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.entries), 1)

    def test_unsupported_lines_are_ignored_not_errors(self):
        result = bulk.parse_config_text(
            "interface=eth0\n"
            "dhcp-range=192.168.1.100,192.168.1.200,24h\n"
            "dhcp-host=AA:BB:CC:00:11:24,192.168.1.63,pc1\n"
            "dhcp-option=3,192.168.1.1"
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(len(result.ignored), 3)

    def test_errors(self):
        cases = [
            ("dhcp-host=ZZ:ZZ:ZZ:ZZ:ZZ:ZZ,192.168.1.73,x", "MAC 地址格式不正确"),
            ("dhcp-host=AA:BB:CC:00:11:26", "缺少 IP 地址"),
        ]
        for text, keyword in cases:
            result = bulk.parse_config_text(text)
            self.assertFalse(result.ok)
            self.assertIn(keyword, result.errors[0]["reason"])

    def test_duplicate_mac_and_ip(self):
        result = bulk.parse_config_text(
            "dhcp-host=AA:BB:CC:00:11:27,192.168.1.70,a\n"
            "dhcp-host=AA:BB:CC:00:11:27,192.168.1.71,b"
        )
        self.assertFalse(result.ok)
        self.assertIn("MAC", result.errors[0]["reason"])

        result = bulk.parse_config_text(
            "dhcp-host=AA:BB:CC:00:11:28,192.168.1.72,a\n"
            "dhcp-host=AA:BB:CC:00:11:29,192.168.1.72,b"
        )
        self.assertFalse(result.ok)
        self.assertIn("IP", result.errors[0]["reason"])

    def test_lease_time_variants(self):
        for lease in ("24h", "7d", "30m", "infinite", "3600"):
            result = bulk.parse_config_text(
                "dhcp-host=AA:BB:CC:00:11:30,192.168.1.80,pc,%s" % lease
            )
            self.assertTrue(result.ok, lease)
            self.assertEqual(result.entries[0]["lease_time"], lease.lower())

    def test_error_line_numbers(self):
        result = bulk.parse_config_text(
            "dhcp-host=AA:BB:CC:00:11:31,192.168.1.81,ok\n"
            "# 注释\n"
            "dhcp-host=bad"
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0]["line"], 3)


class TestRender(unittest.TestCase):
    def test_render_includes_tag_and_lease(self):
        text = bulk.render_static_text([
            {"mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.1.10", "name": "nas",
             "gateway": "", "dns_servers": [], "lease_time": "infinite", "enabled": True},
        ], header=False)
        self.assertEqual(
            text.strip(),
            "dhcp-host=AA:BB:CC:DD:EE:FF,set:host_aabbccddeeff,192.168.1.10,nas,infinite",
        )

    def test_render_gateway_and_dns(self):
        text = bulk.render_static_text([
            {"mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.1.10", "name": "nas",
             "gateway": "192.168.1.254", "dns_servers": ["8.8.8.8"], "lease_time": "",
             "enabled": True},
        ], header=False).strip().splitlines()
        self.assertEqual(len(text), 3)
        self.assertTrue(text[1].startswith("dhcp-option=tag:host_aabbccddeeff,3,"))
        self.assertTrue(text[2].startswith("dhcp-option=tag:host_aabbccddeeff,6,"))

    def test_render_matches_parser_output(self):
        """渲染结果必须能被解析器原样读回。"""
        result = bulk.parse_config_text(USER_SAMPLE)
        rendered = bulk.render_static_text(result.entries, header=False)
        again = bulk.parse_config_text(rendered)
        self.assertTrue(again.ok)
        self.assertEqual(len(again.entries), len(result.entries))


class FakeDnsmasq:
    def __init__(self):
        self.restarts = 0

    def status(self):
        return {"running": True, "pid": 1, "uptime": 1, "restartCount": self.restarts, "lastExit": None}

    def restart(self):
        self.restarts += 1

    def hard_restart(self):
        self.restarts += 1

    def logs(self, limit=200):
        return []


class ImportApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(dnsmasq_conf, "MAIN_CONF", os.path.join(TMP, "dnsmasq.conf")),
            mock.patch.object(dnsmasq_conf, "MANAGED_CONF", os.path.join(TMP, "10-managed.conf")),
        ]
        for item in cls._patches:
            item.start()

        cls.app_config = AppConfig(os.path.join(TMP, "config-bulk.json"))
        cls.app_config.load()
        cls.ctx = api_mod.ApiContext(
            app_config=cls.app_config,
            dnsmasq=FakeDnsmasq(),
            activity=ActivityStore(os.path.join(TMP, "activity-bulk.json")),
            lease_file=os.path.join(TMP, "leases-bulk"),
            version="bulk-test",
        )
        cls.server = api_mod.make_server(cls.ctx, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for item in cls._patches:
            item.stop()

    def setUp(self):
        self.app_config.update(lambda d: d.update({"static_leases": []}))

    def request(self, path, method="GET", payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    # -- 用例 -----------------------------------------------------------

    def test_import_merge(self):
        code, body = self.request(
            "/api/static/import", "POST", {"text": USER_SAMPLE, "mode": "merge"}
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["data"]["count"], 8)

        statics = self.request("/api/static")[1]["data"]
        self.assertEqual(len(statics), 8)
        by_mac = {item["mac"]: item for item in statics}
        self.assertEqual(by_mac["6E:53:C6:69:A9:DA"]["ip"], "192.168.1.21")
        self.assertEqual(by_mac["6E:53:C6:69:A9:DA"]["lease_time"], "infinite")
        self.assertEqual(by_mac["F8:0F:41:53:06:0E"]["name"], "pc-liuyingmei")

    def test_generated_conf_after_import(self):
        self.request("/api/static/import", "POST", {"text": USER_SAMPLE, "mode": "replace"})
        managed = dnsmasq_conf.render_managed(self.app_config.snapshot())
        self.assertIn(
            "dhcp-host=6E:53:C6:69:A9:DA,set:host_6e53c669a9da,192.168.1.21,fnos,infinite",
            managed,
        )
        self.assertIn(
            "dhcp-host=A0:8C:FD:DF:E0:8D,set:host_a08cfddfe08d,192.168.1.40,pc-lvwei",
            managed,
        )

    def test_merge_updates_existing_mac(self):
        self.request(
            "/api/static", "POST",
            {"mac": "6E:53:C6:69:A9:DA", "ip": "192.168.1.99", "name": "old-name"},
        )
        self.request("/api/static/import", "POST", {"text": USER_SAMPLE, "mode": "merge"})
        statics = self.request("/api/static")[1]["data"]
        self.assertEqual(len(statics), 8)
        updated = [i for i in statics if i["mac"] == "6E:53:C6:69:A9:DA"][0]
        self.assertEqual(updated["ip"], "192.168.1.21")
        self.assertEqual(updated["name"], "fnos")

    def test_replace_drops_old_entries(self):
        self.request(
            "/api/static", "POST",
            {"mac": "DE:AD:BE:EF:00:01", "ip": "192.168.1.200", "name": "stale"},
        )
        self.request("/api/static/import", "POST", {"text": USER_SAMPLE, "mode": "replace"})
        statics = self.request("/api/static")[1]["data"]
        self.assertEqual(len(statics), 8)
        self.assertNotIn("DE:AD:BE:EF:00:01", [i["mac"] for i in statics])

    def test_replace_keeps_disabled_entries(self):
        self.request(
            "/api/static", "POST",
            {"mac": "DE:AD:BE:EF:00:02", "ip": "192.168.1.201", "name": "paused",
             "enabled": False},
        )
        self.request("/api/static/import", "POST", {"text": USER_SAMPLE, "mode": "replace"})
        statics = self.request("/api/static")[1]["data"]
        self.assertEqual(len(statics), 9)
        self.assertIn("DE:AD:BE:EF:00:02", [i["mac"] for i in statics])

    def test_ip_conflict_with_existing(self):
        self.request(
            "/api/static", "POST",
            {"mac": "DE:AD:BE:EF:00:03", "ip": "192.168.1.21", "name": "occupied"},
        )
        code, body = self.request(
            "/api/static/import", "POST", {"text": USER_SAMPLE, "mode": "merge"}
        )
        self.assertEqual(code, 400, body)
        self.assertIn("冲突", body["message"])
        self.assertIn("192.168.1.21", body.get("detail", ""))

    def test_syntax_errors_rejected(self):
        code, body = self.request(
            "/api/static/import", "POST",
            {"text": "dhcp-host=ZZ:ZZ:ZZ:ZZ:ZZ:ZZ,192.168.1.73,x", "mode": "merge"},
        )
        self.assertEqual(code, 400, body)
        self.assertIn("错误", body["message"])
        self.assertEqual(len(self.request("/api/static")[1]["data"]), 0)

    def test_bad_mode_rejected(self):
        code, body = self.request(
            "/api/static/import", "POST", {"text": USER_SAMPLE, "mode": "explode"}
        )
        self.assertEqual(code, 400, body)
        self.assertIn("模式", body["message"])

    def test_empty_text_rejected(self):
        code, _ = self.request("/api/static/import", "POST", {"text": "# 只有注释", "mode": "merge"})
        self.assertEqual(code, 400)

    def test_rollback_on_invalid_entry(self):
        """导入失败时不能留下半成品数据。"""
        before = self.request("/api/static")[1]["data"]
        self.request("/api/static/import", "POST", {"text": "dhcp-host=bad-line", "mode": "replace"})
        after = self.request("/api/static")[1]["data"]
        self.assertEqual(before, after)

    def test_preview_does_not_save(self):
        parsed = self.request("/api/static/preview", "POST", {"text": USER_SAMPLE, "mode": "merge"})
        self.assertEqual(parsed[0], 200)
        self.assertTrue(parsed[1]["data"]["valid"])
        self.assertEqual(parsed[1]["data"]["stats"]["hosts"], 8)
        self.assertEqual(len(self.request("/api/static")[1]["data"]), 0)

    def test_preview_reports_errors_and_conflicts(self):
        data = self.request(
            "/api/static/preview", "POST", {"text": "dhcp-host=nonsense", "mode": "merge"}
        )[1]["data"]
        self.assertFalse(data["valid"])
        self.assertTrue(data["errors"])

        self.request(
            "/api/static", "POST",
            {"mac": "DE:AD:BE:EF:00:04", "ip": "192.168.1.21", "name": "occupied"},
        )
        data = self.request(
            "/api/static/preview", "POST", {"text": USER_SAMPLE, "mode": "merge"}
        )[1]["data"]
        self.assertFalse(data["valid"])
        self.assertTrue(data["conflicts"])

    def test_export_round_trip(self):
        self.request("/api/static/import", "POST", {"text": USER_SAMPLE, "mode": "replace"})
        exported = self.request("/api/static/export")[1]["data"]
        self.assertEqual(exported["count"], 8)

        # 清空后用导出的内容重新导入，应得到相同结果
        before = self.request("/api/static")[1]["data"]
        self.app_config.update(lambda d: d.update({"static_leases": []}))
        code, body = self.request(
            "/api/static/import", "POST", {"text": exported["text"], "mode": "replace"}
        )
        self.assertEqual(code, 200, body)
        after = self.request("/api/static")[1]["data"]
        self.assertEqual(
            [(i["mac"], i["ip"], i["name"], i["lease_time"]) for i in before],
            [(i["mac"], i["ip"], i["name"], i["lease_time"]) for i in after],
        )

    def test_export_skips_disabled(self):
        self.request(
            "/api/static", "POST",
            {"mac": "DE:AD:BE:EF:00:05", "ip": "192.168.1.205", "name": "paused", "enabled": False},
        )
        self.request(
            "/api/static", "POST",
            {"mac": "DE:AD:BE:EF:00:06", "ip": "192.168.1.206", "name": "active"},
        )
        text = self.request("/api/static/export")[1]["data"]["text"]
        self.assertIn("192.168.1.206", text)
        self.assertNotIn("192.168.1.205", text)


class TestCustomOptions(unittest.TestCase):
    def test_rendered_into_managed_conf(self):
        from app.config import DEFAULT_CONFIG

        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["dhcp"]["custom_options"] = "dhcp-option=42,192.168.1.1\n\n# 注释行"
        text = dnsmasq_conf.render_managed(config)
        self.assertIn("# ---- 自定义配置", text)
        self.assertIn("dhcp-option=42,192.168.1.1", text)
        # 用户写的注释保留，空行去掉
        self.assertIn("注释行", text)
        self.assertNotIn("dhcp-option=42,192.168.1.1\n\n", text)

    def test_empty_custom_options(self):
        from app.config import DEFAULT_CONFIG

        text = dnsmasq_conf.render_managed(json.loads(json.dumps(DEFAULT_CONFIG)))
        self.assertNotIn("自定义配置", text)

    def test_banned_options_rejected(self):
        from app.config import validate_dhcp_block

        for banned in ("conf-dir=/etc/other", "dhcp-leasefile=/tmp/x", "dhcp-script=/bin/sh"):
            with self.assertRaises(ValidationError):
                validate_dhcp_block({
                    "range_start": "192.168.1.100",
                    "range_end": "192.168.1.200",
                    "custom_options": banned,
                })


if __name__ == "__main__":
    unittest.main(verbosity=2)
