"""解析 dnsmasq 租约文件，计算剩余租期、判定静态/动态与在线状态。

dnsmasq.leases 每行格式（空格分隔）：
    <过期时间epoch|0> <MAC> <IP> <主机名|*> <client-id|*>
部分版本还会在末尾追加 DUID / IAID 字段。
"""

from __future__ import annotations

import ipaddress
import os
import time
from typing import Any, Dict, List, Optional

from .config import normalize_mac


def format_remaining(expiry: int, now: Optional[int] = None) -> Dict[str, Any]:
    """把过期时间换算成剩余租期描述。"""
    now = now if now is not None else int(time.time())
    if expiry <= 0:
        return {"seconds": -1, "text": "永久", "expired": False, "permanent": True}

    left = expiry - now
    if left <= 0:
        return {"seconds": 0, "text": "已过期", "expired": True, "permanent": False}

    days, rem = divmod(left, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        text = "%d天%d小时" % (days, hours)
    elif hours:
        text = "%d小时%d分" % (hours, minutes)
    elif minutes:
        text = "%d分%d秒" % (minutes, seconds)
    else:
        text = "%d秒" % seconds
    return {"seconds": left, "text": text, "expired": False, "permanent": False}


def parse_lease_file(path: str) -> List[Dict[str, Any]]:
    """读取租约文件，返回原始条目列表（不做静态判定）。"""
    rows: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return rows
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        return rows

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            expiry = int(parts[0])
        except ValueError:
            continue
        mac = normalize_mac(parts[1])
        ip = parts[2]
        hostname = parts[3] if len(parts) > 3 else "*"
        client_id = parts[4] if len(parts) > 4 else "*"
        rows.append(
            {
                "expiry": expiry,
                "mac": mac,
                "ip": ip,
                "hostname": "" if hostname in ("*", "") else hostname,
                "clientId": "" if client_id in ("*", "") else client_id,
            }
        )
    return rows


def ip_sort_key(ip: str):
    try:
        return (0, int(ipaddress.IPv4Address(ip)))
    except Exception:
        return (1, 0)


def build_leases(
    lease_file: str,
    static_leases: List[Dict[str, Any]],
    activity: Optional[Dict[str, Any]] = None,
    online_window: int = 900,
    lease_time_seconds: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """合并租约文件与静态绑定，输出前端所需的完整列表。

    - 静态绑定但当前无租约的设备仍会列出（状态为离线/未分配）
    - 在线判定：最后活跃时间在窗口内，或租约剩余时间接近满租期
    """
    activity = activity or {}
    now = int(time.time())
    static_index = {
        normalize_mac(item.get("mac", "")): item for item in static_leases if item.get("mac")
    }

    # 在线窗口：取配置窗口与半个租期的较大值，避免长租期下误判离线
    window = online_window or 900
    if lease_time_seconds and lease_time_seconds > 0:
        window = max(window, int(lease_time_seconds / 2))

    result: List[Dict[str, Any]] = []
    seen_macs = set()

    for row in parse_lease_file(lease_file):
        mac = row["mac"]
        seen_macs.add(mac)
        static = static_index.get(mac)
        last_seen = activity.get(mac, 0)
        remaining = format_remaining(row["expiry"], now)

        online = bool(last_seen and (now - last_seen) <= window)
        if not online and not remaining["expired"] and last_seen == 0:
            # 没有活动记录的旧租约，用剩余租期粗判
            online = remaining["permanent"] or (
                lease_time_seconds is not None
                and remaining["seconds"] > lease_time_seconds * 0.5
            )

        result.append(
            {
                "mac": mac,
                "ip": row["ip"],
                "hostname": (static or {}).get("name") or row["hostname"],
                "clientId": row["clientId"],
                "type": "static" if static else "dynamic",
                "isStatic": bool(static),
                "expiry": row["expiry"],
                "expireAt": "" if row["expiry"] <= 0 else time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(row["expiry"])
                ),
                "remaining": remaining,
                "online": online,
                "lastSeen": last_seen,
                "lastSeenText": _ago(last_seen, now),
                "note": (static or {}).get("note", ""),
                "active": (static or {}).get("enabled", True) if static else True,
            }
        )

    # 静态绑定但尚未出现在租约文件中
    for mac, item in static_index.items():
        if mac in seen_macs:
            continue
        last_seen = activity.get(mac, 0)
        result.append(
            {
                "mac": mac,
                "ip": item.get("ip", ""),
                "hostname": item.get("name", ""),
                "clientId": "",
                "type": "static",
                "isStatic": True,
                "expiry": 0,
                "expireAt": "",
                "remaining": {"seconds": -1, "text": "未分配", "expired": False, "permanent": True},
                "online": bool(last_seen and (now - last_seen) <= window),
                "lastSeen": last_seen,
                "lastSeenText": _ago(last_seen, now),
                "note": item.get("note", ""),
                "active": item.get("enabled", True),
            }
        )

    result.sort(key=lambda r: ip_sort_key(r["ip"]))
    return result


def _ago(timestamp: int, now: int) -> str:
    if not timestamp:
        return "从未"
    delta = now - timestamp
    if delta < 0:
        return "刚刚"
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return "%d 分钟前" % (delta // 60)
    if delta < 86400:
        return "%d 小时前" % (delta // 3600)
    return "%d 天前" % (delta // 86400)


def summarize(leases: List[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "total": len(leases),
        "online": sum(1 for item in leases if item.get("online")),
        "static": sum(1 for item in leases if item.get("isStatic")),
        "dynamic": sum(1 for item in leases if not item.get("isStatic")),
    }
