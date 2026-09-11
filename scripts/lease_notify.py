#!/usr/bin/env python3
"""dnsmasq dhcp-script 回调：记录客户端最后活跃时间。

由 dnsmasq 以如下形式调用：
    lease_notify.py <add|old|del> <mac> <ip> <hostname> [client-id]
"""

import os
import sys

sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

from app.activity import ActivityStore, handle_dhcp_event  # noqa: E402


def main() -> int:
    path = os.environ.get("ACTIVITY_FILE", "/data/activity.json")
    store = ActivityStore(path)
    store.load()
    handle_dhcp_event(store, sys.argv[1:])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 回调失败不能影响 dnsmasq 分配地址
        sys.stderr.write("lease_notify error: %s\n" % exc)
        sys.exit(0)
