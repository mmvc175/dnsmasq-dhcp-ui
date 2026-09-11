#!/usr/bin/env python3
"""容器健康检查：确认 Web 管理接口可响应（开启认证时 401 也算健康）。"""

import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    port = os.environ.get("WEB_PORT", "8080")
    url = "http://127.0.0.1:%s/api/leases" % port
    code = 0
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception:
        code = 0
    return 0 if code in (200, 401) else 1


if __name__ == "__main__":
    sys.exit(main())
