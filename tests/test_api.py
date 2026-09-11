"""端到端冒烟测试：起真实 HTTP 服务，走一遍全部接口。

dnsmasq 二进制与 /etc 写入在测试环境不可用时会被替换成桩对象，
因此该测试只验证 API 层与配置渲染的联动是否正确。
"""

import base64
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="dhcpui-test-")
os.environ["DATA_DIR"] = TMP
os.environ["HOSTS_FILE"] = os.path.join(TMP, "hosts.dnsmasq")
os.environ["LEASE_SCRIPT"] = os.path.join(ROOT, "scripts", "lease_notify.py")

from unittest import mock

from app import api as api_mod
from app import dnsmasq_conf
from app.activity import ActivityStore
from app.config import AppConfig

RENDERED = {}


def fake_apply(config, lease_file, hosts_file, script):
    managed = dnsmasq_conf.render_managed(config)
    with open(dnsmasq_conf.MANAGED_CONF, "w", encoding="utf-8") as handle:
        handle.write(managed)
    with open(hosts_file, "w", encoding="utf-8") as handle:
        handle.write(dnsmasq_conf.render_hosts(config))
    RENDERED["managed"] = managed
    return managed


class FakeDnsmasq:
    def __init__(self):
        self.restarts = 0
        self.hard = 0
        self._logs = ["dnsmasq: started, version 2.90 cachesize 1500"]

    def status(self):
        return {"running": True, "pid": 4242, "uptime": 12, "restartCount": self.restarts, "lastExit": None}

    def restart(self):
        self.restarts += 1

    def hard_restart(self):
        self.hard += 1
        self.restarts += 1

    def logs(self, limit=200):
        return self._logs[-limit:]


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 用 patch 而不是直接赋值，避免污染其它测试模块
        cls._patches = [
            mock.patch.object(dnsmasq_conf, "MAIN_CONF", os.path.join(TMP, "dnsmasq.conf")),
            mock.patch.object(dnsmasq_conf, "MANAGED_CONF", os.path.join(TMP, "10-managed.conf")),
            mock.patch.object(dnsmasq_conf, "apply", fake_apply),
        ]
        for item in cls._patches:
            item.start()

        cls.lease_file = os.path.join(TMP, "dnsmasq.leases")
        with open(cls.lease_file, "w", encoding="utf-8") as handle:
            now = int(__import__("time").time())
            handle.write("%d aa:bb:cc:dd:ee:ff 192.168.1.10 nas *\n" % (now + 3600))
            handle.write("%d 11:22:33:44:55:66 192.168.1.101 * *\n" % (now + 7200))

        cls.app_config = AppConfig(os.path.join(TMP, "config.json"))
        cls.app_config.load()
        cls.activity = ActivityStore(os.path.join(TMP, "activity.json"))
        cls.activity.load()
        cls.activity.touch("AA:BB:CC:DD:EE:FF")
        cls.dnsmasq = FakeDnsmasq()

        cls.ctx = api_mod.ApiContext(
            app_config=cls.app_config,
            dnsmasq=cls.dnsmasq,
            activity=cls.activity,
            lease_file=cls.lease_file,
            version="test",
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
        """每个用例都从干净状态开始，避免相互污染。"""
        self.app_config.update(lambda d: d.update({"static_leases": []}))

    # -- 工具 -----------------------------------------------------------

    def request(self, path, method="GET", payload=None, auth=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth:
            token = base64.b64encode(("%s:%s" % auth).encode("utf-8")).decode()
            req.add_header("Authorization", "Basic " + token)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(body)
            except Exception:
                return exc.code, {"raw": body}

    # -- 用例 -----------------------------------------------------------

    def test_state(self):
        code, body = self.request("/api/state")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        data = body["data"]
        self.assertEqual(data["summary"]["total"], 2)
        self.assertGreaterEqual(data["summary"]["online"], 1)
        self.assertTrue(data["service"]["running"])
        self.assertIn("dhcp", data["config"])

        first = [r for r in data["leases"] if r["mac"] == "AA:BB:CC:DD:EE:FF"][0]
        self.assertTrue(first["online"])
        self.assertRegex(first["remaining"]["text"], r"小时|分")

    def test_add_static_and_rendered(self):
        code, body = self.request(
            "/api/static",
            "POST",
            {"name": "printer", "mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.1.88",
             "gateway": "192.168.1.254", "dns_servers": ["8.8.8.8"]},
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["data"]["ip"], "192.168.1.88")

        managed = RENDERED["managed"]
        self.assertIn("dhcp-host=AA:BB:CC:DD:EE:FF,set:host_aabbccddeeff,192.168.1.88,printer", managed)
        self.assertIn("dhcp-option=tag:host_aabbccddeeff,3,192.168.1.254", managed)
        self.assertIn("dhcp-option=tag:host_aabbccddeeff,6,8.8.8.8", managed)

        # 状态里应标记为静态且显示配置过的设备名
        code, body = self.request("/api/state")
        row = [r for r in body["data"]["leases"] if r["mac"] == "AA:BB:CC:DD:EE:FF"][0]
        self.assertTrue(row["isStatic"])
        self.assertEqual(row["hostname"], "printer")

    def test_duplicate_ip_rejected(self):
        self.request("/api/static", "POST", {"mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.1.88"})
        code, body = self.request(
            "/api/static", "POST",
            {"mac": "22:22:33:44:55:66", "ip": "192.168.1.88"},
        )
        self.assertEqual(code, 400, body)
        self.assertIn("占用", body["message"])

    def test_update_existing_static(self):
        self.request("/api/static", "POST", {"mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.1.10", "name": "nas"})
        code, body = self.request(
            "/api/static", "POST",
            {"mac": "aa-bb-cc-dd-ee-ff", "ip": "192.168.1.99", "name": "nas"},
        )
        self.assertEqual(code, 200, body)
        statics = self.request("/api/static")[1]["data"]
        self.assertEqual(len(statics), 1)
        self.assertEqual(statics[0]["ip"], "192.168.1.99")
        self.assertEqual(statics[0]["mac"], "AA:BB:CC:DD:EE:FF")

    def test_delete_static(self):
        self.request("/api/static", "POST", {"mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.1.66"})
        code, body = self.request("/api/static/delete", "POST", {"mac": "AA:BB:CC:DD:EE:FF"})
        self.assertEqual(code, 200, body)
        self.assertEqual(self.request("/api/static")[1]["data"], [])

        code, body = self.request("/api/static/delete", "POST", {"mac": "AA:BB:CC:DD:EE:FF"})
        self.assertEqual(code, 400)

    def test_invalid_mac_rejected(self):
        code, _ = self.request("/api/static", "POST", {"mac": "not-a-mac", "ip": "192.168.1.70"})
        self.assertEqual(code, 400)

    def test_save_dhcp_config(self):
        code, body = self.request(
            "/api/config", "POST",
            {"dhcp": {
                "enabled": True,
                "interface": "eth0",
                "range_start": "10.0.0.100",
                "range_end": "10.0.0.200",
                "netmask": "/24",
                "lease_time": "12h",
                "gateway": "10.0.0.1",
                "dns_servers": "10.0.0.1, 223.5.5.5",
                "domain": "home",
                "authoritative": True,
            }},
        )
        self.assertEqual(code, 200, body)
        self.assertIn("dhcp-range=10.0.0.100,10.0.0.200,255.255.255.0,12h", RENDERED["managed"])
        self.assertIn("dhcp-option=6,10.0.0.1,223.5.5.5", RENDERED["managed"])
        self.assertIn("domain=home", RENDERED["managed"])
        self.assertEqual(self.dnsmasq.hard > 0, True)

    def test_partial_update_keeps_other_section(self):
        """只提交 dns 时，dhcp 段落必须保持原值。"""
        self.request(
            "/api/config", "POST",
            {"dhcp": {"range_start": "172.16.9.10", "range_end": "172.16.9.99",
                      "lease_time": "8h", "gateway": "172.16.9.1", "dns_servers": []}},
        )
        code, body = self.request(
            "/api/config", "POST",
            {"dns": {"enabled": True, "no_resolv": True, "forwarders": ["9.9.9.9"]}},
        )
        self.assertEqual(code, 200, body)
        config = self.request("/api/config")[1]["data"]
        self.assertEqual(config["dhcp"]["range_start"], "172.16.9.10")
        self.assertEqual(config["dhcp"]["lease_time"], "8h")
        self.assertEqual(config["dns"]["forwarders"], ["9.9.9.9"])

    def test_bad_range_rejected(self):
        code, _ = self.request(
            "/api/config", "POST",
            {"dhcp": {"range_start": "10.0.0.200", "range_end": "10.0.0.100"}},
        )
        self.assertEqual(code, 400)

    def test_logs_and_raw(self):
        code, body = self.request("/api/logs")
        self.assertEqual(code, 200)
        self.assertIn("dnsmasq", body["data"]["logs"][0])

        code, body = self.request("/api/raw")
        self.assertEqual(code, 200)
        self.assertIn("dhcp-range=", body["data"]["managed"])

    def test_static_index_page(self):
        req = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(req, timeout=5) as response:
            html = response.read().decode("utf-8")
        self.assertIn("DHCP / DNS 管理台", html)
        self.assertEqual(response.status, 200)

        req = urllib.request.Request(self.base + "/app.js")
        with urllib.request.urlopen(req, timeout=5) as response:
            self.assertIn("api('/api/state')", response.read().decode("utf-8"))

    def test_path_traversal_blocked(self):
        req = urllib.request.Request(self.base + "/../Dockerfile")
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            self.assertIn(exc.code, (403, 404))
        else:
            self.fail("路径穿越未被拦截")

    def test_auth_flow(self):
        self.app_config.update(lambda d: d["auth"].update({"enabled": True, "username": "admin", "password": "pw"}))
        try:
            code, body = self.request("/api/state")
            self.assertEqual(code, 401, body)

            code, body = self.request("/api/state", auth=("admin", "wrong"))
            self.assertEqual(code, 401)

            code, body = self.request("/api/state", auth=("admin", "pw"))
            self.assertEqual(code, 200, body)
        finally:
            self.app_config.update(lambda d: d["auth"].update({"enabled": False}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
