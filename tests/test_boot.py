"""启动流程测试：验证 bootstrap 能生成配置文件，且入口可正常拉起 Web 服务。

dnsmasq 二进制在开发环境不存在，因此用桩对象替换进程管理器。
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="dhcpui-boot-")

_SAVED_ENV = {}
for key in ("DATA_DIR", "HOSTS_FILE", "LEASE_SCRIPT", "WEB_PORT", "DHCP_RANGE_START"):
    _SAVED_ENV[key] = os.environ.get(key)

os.environ["DATA_DIR"] = TMP
os.environ["HOSTS_FILE"] = os.path.join(TMP, "hosts.dnsmasq")
os.environ["LEASE_SCRIPT"] = os.path.join(ROOT, "scripts", "lease_notify.py")
os.environ["WEB_PORT"] = "0"
os.environ["DHCP_RANGE_START"] = "192.168.50.100"
os.environ.pop("DHCP_RANGE_END", None)

from app import dnsmasq_conf, main as main_mod  # noqa: E402

dnsmasq_conf.MAIN_CONF = os.path.join(TMP, "dnsmasq.conf")
dnsmasq_conf.MANAGED_CONF = os.path.join(TMP, "10-managed.conf")


class FakeDnsmasq:
    def __init__(self, *args, **kwargs):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self, *args, **kwargs):
        self.stopped = True

    def restart(self):
        pass

    def hard_restart(self):
        pass

    def status(self):
        return {"running": True, "pid": 1, "uptime": 1, "restartCount": 0, "lastExit": None}

    def logs(self, limit=200):
        return []


class BootTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_config, cls.activity = main_mod.bootstrap()

    @classmethod
    def tearDownClass(cls):
        for key, value in _SAVED_ENV.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_bootstrap_creates_files(self):
        self.assertTrue(os.path.isfile(os.path.join(TMP, "config.json")))
        self.assertTrue(os.path.isfile(os.path.join(TMP, "dnsmasq.leases")))
        # 环境变量传入的初始值应生效
        self.assertEqual(
            self.app_config.snapshot()["dhcp"]["range_start"], "192.168.50.100"
        )

    def test_write_config_produces_managed_conf(self):
        main_mod.write_config(self.app_config)
        with open(dnsmasq_conf.MANAGED_CONF, encoding="utf-8") as handle:
            managed = handle.read()
        self.assertIn("dhcp-range=192.168.50.100", managed)
        with open(dnsmasq_conf.MAIN_CONF, encoding="utf-8") as handle:
            main_conf = handle.read()
        self.assertIn("dhcp-leasefile=%s" % os.path.join(TMP, "dnsmasq.leases"), main_conf)
        self.assertTrue(os.path.isfile(os.path.join(TMP, "hosts.dnsmasq")))

    def test_web_server_boots(self):
        from app.api import ApiContext, make_server

        original = main_mod.DnsmasqProcess
        main_mod.DnsmasqProcess = FakeDnsmasq
        try:
            ctx = ApiContext(
                app_config=self.app_config,
                dnsmasq=FakeDnsmasq(),
                activity=self.activity,
                lease_file=main_mod.LEASE_FILE,
                version="boot-test",
            )
            server = make_server(ctx, "127.0.0.1", 0)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/state" % port, timeout=5
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["data"]["meta"]["version"], "boot-test")
            finally:
                server.shutdown()
                server.server_close()
        finally:
            main_mod.DnsmasqProcess = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
