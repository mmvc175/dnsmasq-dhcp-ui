"""主动探测：并发 ping 一批 IP，用于刷新在线状态。

需要容器具备 CAP_NET_RAW（compose 中已默认授予）。
探测失败不会报错，只作为 dhcp-script 活跃记录的补充。
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

PING_BIN = "ping"


def ping_once(ip: str, timeout: int = 1) -> bool:
    if not ip:
        return False
    try:
        proc = subprocess.run(
            [PING_BIN, "-c", "1", "-W", str(timeout), "-n", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
        )
        return proc.returncode == 0
    except Exception:
        return False


def sweep(ips: List[str], workers: int = 16) -> Dict[str, bool]:
    unique = [ip for ip in dict.fromkeys(ips) if ip]
    if not unique:
        return {}
    results: Dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(unique))) as pool:
        for ip, alive in zip(unique, pool.map(ping_once, unique)):
            results[ip] = alive
    return results
