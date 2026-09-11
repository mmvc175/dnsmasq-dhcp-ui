"""dnsmasq-dhcp-ui 入口：初始化数据目录、渲染配置、拉起 dnsmasq 与 Web 服务。

容器以本模块作为 PID 1 运行，负责：
1. 生成 dnsmasq 配置
2. 守护 dnsmasq 子进程（异常退出自动拉起）
3. 提供 HTTP 管理接口
4. 处理 SIGTERM / SIGINT，优雅退出
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import dnsmasq_conf
from app.activity import ActivityStore
from app.api import ApiContext, make_server
from app.config import AppConfig
from app.dnsmasq_proc import DnsmasqProcess

VERSION = "1.0.0"

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join(DATA_DIR, "config.json"))
LEASE_FILE = os.environ.get("LEASE_FILE", os.path.join(DATA_DIR, "dnsmasq.leases"))
ACTIVITY_FILE = os.environ.get("ACTIVITY_FILE", os.path.join(DATA_DIR, "activity.json"))
HOSTS_FILE = os.environ.get("HOSTS_FILE", os.path.join(DATA_DIR, "hosts.dnsmasq"))
LEASE_SCRIPT = os.environ.get("LEASE_SCRIPT", "/app/scripts/lease_notify.py")

WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))


def bootstrap() -> tuple:
    """初始化目录与配置对象。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(dnsmasq_conf.MANAGED_CONF), exist_ok=True)

    app_config = AppConfig(CONFIG_FILE)
    app_config.load()
    app_config.save()

    activity = ActivityStore(ACTIVITY_FILE)
    activity.load()

    # 租约文件必须存在，否则 dnsmasq 启动时会报警
    if not os.path.exists(LEASE_FILE):
        with open(LEASE_FILE, "a", encoding="utf-8"):
            pass

    return app_config, activity


def write_config(app_config: AppConfig) -> None:
    dnsmasq_conf.apply(
        app_config.snapshot(),
        lease_file=LEASE_FILE,
        hosts_file=HOSTS_FILE,
        script=LEASE_SCRIPT,
    )


def main() -> int:
    app_config, activity = bootstrap()
    write_config(app_config)

    ok, output = dnsmasq_conf.test_config()
    if not ok:
        print("[boot] dnsmasq 配置自检失败：\n%s" % output, file=sys.stderr)
    elif output:
        print("[boot] dnsmasq 配置自检通过")

    dnsmasq = DnsmasqProcess()
    dnsmasq.start()

    ctx = ApiContext(
        app_config=app_config,
        dnsmasq=dnsmasq,
        activity=activity,
        lease_file=LEASE_FILE,
        version=VERSION,
    )
    httpd = make_server(ctx, WEB_HOST, WEB_PORT)
    print("[boot] Web 管理界面已启动：http://0.0.0.0:%d" % WEB_PORT)

    stop_event = threading.Event()

    def on_signal(signum, frame):
        print("[boot] 收到信号 %s，正在退出…" % signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    def housekeeping():
        while not stop_event.is_set():
            time.sleep(60)
            try:
                activity.prune()
                activity.flush()
            except Exception as exc:
                print("[boot] 活跃记录维护失败：%s" % exc)

    threading.Thread(target=housekeeping, daemon=True).start()

    try:
        while not stop_event.wait(1.0):
            if not dnsmasq.running:
                # _watch 线程会自愈，这里只做兜底
                pass
    except KeyboardInterrupt:
        stop_event.set()

    httpd.shutdown()
    httpd.server_close()
    dnsmasq.stop()
    try:
        activity.flush()
    except Exception:
        pass
    print("[boot] 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
