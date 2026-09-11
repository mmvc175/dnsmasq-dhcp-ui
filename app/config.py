"""应用配置模型：默认值、环境变量初始化、持久化与校验。

配置文件以 JSON 存放在数据目录（默认 /data/config.json），
挂载该目录即可在容器重建后保留全部设置。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import threading
from typing import Any, Dict, List, Optional

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
LEASE_TIME_RE = re.compile(r"^(infinite|\d+[smhdw]?)$", re.IGNORECASE)

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "dhcp": {
        "enabled": True,
        "interface": "",
        "range_start": "192.168.1.100",
        "range_end": "192.168.1.200",
        "netmask": "255.255.255.0",
        "lease_time": "24h",
        "gateway": "192.168.1.1",
        "dns_servers": ["192.168.1.1", "223.5.5.5"],
        "domain": "lan",
        "authoritative": True,
        "custom_options": "",
    },
    "dns": {
        "enabled": True,
        "forwarders": ["223.5.5.5", "114.114.114.114"],
        "no_resolv": True,
    },
    "static_leases": [],
    "ui": {
        "online_window": 900,
    },
    "auth": {
        "enabled": False,
        "username": "admin",
        "password": "",
    },
}


class ValidationError(Exception):
    """配置校验失败。"""


# ---------------------------------------------------------------------------
# 基础校验工具
# ---------------------------------------------------------------------------


def is_valid_mac(value: str) -> bool:
    return bool(value) and bool(MAC_RE.match(value.strip()))


def normalize_mac(value: str) -> str:
    """把各种写法统一成大写冒号分隔，便于比较。"""
    raw = re.sub(r"[^0-9A-Fa-f]", "", value or "")
    if len(raw) != 12:
        return (value or "").strip().upper()
    return ":".join(raw[i:i + 2] for i in range(0, 12, 2)).upper()


def is_valid_ip(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value.strip())
        return True
    except Exception:
        return False


def is_valid_lease_time(value: str) -> bool:
    return bool(value) and bool(LEASE_TIME_RE.match(str(value).strip()))


def parse_ip_list(value: Any) -> List[str]:
    """接受列表或逗号/空格分隔字符串，输出 IP 列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = re.split(r"[,\s]+", str(value).strip())
    return [i for i in items if i]


def validate_ip_list(values: List[str], field: str) -> List[str]:
    for item in values:
        if not is_valid_ip(item):
            raise ValidationError("%s 含无效 IP：%s" % (field, item))
    return values


def normalize_netmask(value: str) -> str:
    """支持 255.255.255.0 或 /24 两种写法，统一输出点分十进制。"""
    text = (value or "").strip()
    if not text:
        return ""
    if text.startswith("/"):
        try:
            return str(ipaddress.IPv4Network("0.0.0.0" + text).netmask)
        except Exception:
            raise ValidationError("子网掩码格式不正确：%s" % value)
    if text.isdigit():
        try:
            return str(ipaddress.IPv4Network("0.0.0.0/" + text).netmask)
        except Exception:
            raise ValidationError("子网掩码格式不正确：%s" % value)
    try:
        ipaddress.IPv4Address(text)
    except Exception:
        raise ValidationError("子网掩码格式不正确：%s" % value)
    return text


def ensure_netmask(netmask: str, range_start: str) -> str:
    """未填写掩码时，按 A/B/C 类地址推断。"""
    if normalize_netmask(netmask):
        return normalize_netmask(netmask)
    try:
        addr = ipaddress.IPv4Address(range_start)
    except Exception:
        return ""
    if addr.is_private and ipaddress.IPv4Address(range_start) in ipaddress.IPv4Network("10.0.0.0/8"):
        return "255.0.0.0"
    if ipaddress.IPv4Address(range_start) in ipaddress.IPv4Network("172.16.0.0/12"):
        return "255.255.0.0"
    return "255.255.255.0"


# ---------------------------------------------------------------------------
# 配置对象
# ---------------------------------------------------------------------------


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """以 base 为骨架，把 override 中的已知字段填进去。"""
    result = dict(base)
    for key, value in (override or {}).items():
        if key not in result:
            result[key] = value
        elif isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class AppConfig:
    """线程安全的配置容器。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))
        self._apply_env_defaults(self._data)

    # -- 加载 / 保存 -----------------------------------------------------

    def load(self) -> Dict[str, Any]:
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as handle:
                        stored = json.load(handle)
                    merged = _deep_merge(self._data, stored)
                    merged["static_leases"] = stored.get("static_leases", [])
                    self._data = merged
                except Exception as exc:  # 配置损坏时回退到默认值
                    print("[config] 读取配置失败（%s），使用默认配置" % exc)
            self._apply_env_overrides(self._data)
            return self._data

    def save(self) -> None:
        with self._lock:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

    # -- 访问 -----------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def update(self, mutator) -> Dict[str, Any]:
        """在锁内修改配置并落盘。mutator(data) 可直接改动字典。"""
        with self._lock:
            mutator(self._data)
            self.save()
            return json.loads(json.dumps(self._data))

    @property
    def dhcp(self) -> Dict[str, Any]:
        return self._data["dhcp"]

    @property
    def static_leases(self) -> List[Dict[str, Any]]:
        return self._data["static_leases"]

    def mac_index(self) -> Dict[str, Dict[str, Any]]:
        """MAC -> 静态绑定条目。"""
        return {
            normalize_mac(item.get("mac", "")): item
            for item in self.static_leases
            if item.get("mac")
        }

    # -- 环境变量 -------------------------------------------------------

    @staticmethod
    def _env_list(name: str) -> Optional[List[str]]:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return None
        return parse_ip_list(raw)

    def _apply_env_defaults(self, data: Dict[str, Any]) -> None:
        """环境变量作为初始值；已存在的 config.json 优先。"""
        dhcp = data["dhcp"]
        dns = data["dns"]
        dhcp["interface"] = os.environ.get("DHCP_INTERFACE", dhcp["interface"])
        dhcp["range_start"] = os.environ.get("DHCP_RANGE_START", dhcp["range_start"])
        dhcp["range_end"] = os.environ.get("DHCP_RANGE_END", dhcp["range_end"])
        dhcp["netmask"] = os.environ.get("DHCP_NETMASK", dhcp["netmask"])
        dhcp["lease_time"] = os.environ.get("DHCP_LEASE_TIME", dhcp["lease_time"])
        dhcp["gateway"] = os.environ.get("DHCP_GATEWAY", dhcp["gateway"])
        dhcp["domain"] = os.environ.get("DHCP_DOMAIN", dhcp["domain"])
        dhcp_list = self._env_list("DHCP_DNS")
        if dhcp_list:
            dhcp["dns_servers"] = dhcp_list
        fwd = self._env_list("DNS_FORWARDERS")
        if fwd:
            dns["forwarders"] = fwd
        if os.environ.get("DNS_ENABLED"):
            dns["enabled"] = os.environ["DNS_ENABLED"].strip() not in ("0", "false", "no")
        data["ui"]["online_window"] = int(
            os.environ.get("ONLINE_WINDOW", data["ui"]["online_window"])
        )

    def _apply_env_overrides(self, data: Dict[str, Any]) -> None:
        """认证信息始终以环境变量为准，避免把口令写进配置文件。"""
        user = os.environ.get("WEB_USER")
        passwd = os.environ.get("WEB_PASS")
        if user or passwd:
            data["auth"]["enabled"] = True
            if user:
                data["auth"]["username"] = user
            if passwd:
                data["auth"]["password"] = passwd
        elif os.environ.get("WEB_AUTH", "").strip() in ("0", "false", "no"):
            data["auth"]["enabled"] = False


# ---------------------------------------------------------------------------
# 业务校验
# ---------------------------------------------------------------------------


def validate_dhcp_block(dhcp: Dict[str, Any]) -> Dict[str, Any]:
    """校验并规范化 DHCP 全局配置。"""
    block = {
        "enabled": bool(dhcp.get("enabled", True)),
        "interface": (dhcp.get("interface") or "").strip(),
        "range_start": (dhcp.get("range_start") or "").strip(),
        "range_end": (dhcp.get("range_end") or "").strip(),
        "netmask": (dhcp.get("netmask") or "").strip(),
        "lease_time": (dhcp.get("lease_time") or "").strip() or "24h",
        "gateway": (dhcp.get("gateway") or "").strip(),
        "dns_servers": validate_ip_list(parse_ip_list(dhcp.get("dns_servers")), "DHCP DNS"),
        "domain": (dhcp.get("domain") or "").strip(),
        "authoritative": bool(dhcp.get("authoritative", True)),
        "custom_options": (dhcp.get("custom_options") or "").strip(),
    }

    if not is_valid_ip(block["range_start"]):
        raise ValidationError("起始 IP 格式不正确")
    if not is_valid_ip(block["range_end"]):
        raise ValidationError("结束 IP 格式不正确")
    if ipaddress.IPv4Address(block["range_start"]) > ipaddress.IPv4Address(block["range_end"]):
        raise ValidationError("起始 IP 不能大于结束 IP")
    if block["gateway"] and not is_valid_ip(block["gateway"]):
        raise ValidationError("网关格式不正确")
    if not is_valid_lease_time(block["lease_time"]):
        raise ValidationError("租期格式不正确（示例：24h、7d、infinite）")
    if len(block["dns_servers"]) > 8:
        raise ValidationError("DNS 服务器最多 8 个")

    block["netmask"] = ensure_netmask(block["netmask"], block["range_start"])
    _guard_custom_options(block["custom_options"])
    return block


BANNED_OPTIONS = (
    "conf-file",
    "conf-dir",
    "dhcp-leasefile",
    "dhcp-script",
    "leasefile-ro",
    "addn-hosts",
    "servers-file",
    "hostsdir",
)


def _guard_custom_options(text: str) -> None:
    """禁止在自定义片段里覆盖系统托管的关键设置。"""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lowered = stripped.lower()
        for banned in BANNED_OPTIONS:
            if lowered.startswith(banned):
                raise ValidationError(
                    "自定义配置不能包含 %s（由系统统一管理）：%s" % (banned, stripped)
                )


def validate_dns_block(dns: Dict[str, Any]) -> Dict[str, Any]:
    block = {
        "enabled": bool(dns.get("enabled", True)),
        "forwarders": validate_ip_list(parse_ip_list(dns.get("forwarders")), "上游 DNS"),
        "no_resolv": bool(dns.get("no_resolv", True)),
    }
    if len(block["forwarders"]) > 8:
        raise ValidationError("上游 DNS 最多 8 个")
    return block


def validate_static_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """校验单条静态绑定。"""
    mac = normalize_mac((entry.get("mac") or "").strip())
    if not is_valid_mac(mac):
        raise ValidationError("MAC 格式不正确（示例 AA:BB:CC:DD:EE:FF）")

    ip = (entry.get("ip") or "").strip()
    if not is_valid_ip(ip):
        raise ValidationError("IP 格式不正确")

    name = (entry.get("name") or "").strip()
    if name and not re.match(r"^[A-Za-z0-9]([A-Za-z0-9\-_.]*[A-Za-z0-9])?$", name):
        raise ValidationError("设备名只能包含字母、数字、-、_ 和 .")

    gateway = (entry.get("gateway") or "").strip()
    if gateway and not is_valid_ip(gateway):
        raise ValidationError("网关格式不正确")

    dns_servers = validate_ip_list(parse_ip_list(entry.get("dns_servers")), "设备 DNS")
    if len(dns_servers) > 8:
        raise ValidationError("DNS 服务器最多 8 个")

    lease_time = (entry.get("lease_time") or "").strip()
    if lease_time and not is_valid_lease_time(lease_time):
        raise ValidationError("租期格式不正确（示例：24h、7d、infinite）")

    return {
        "name": name,
        "mac": mac,
        "ip": ip,
        "gateway": gateway,
        "dns_servers": dns_servers,
        "lease_time": lease_time,
        "enabled": bool(entry.get("enabled", True)),
        "note": (entry.get("note") or "").strip(),
    }
