"""dnsmasq 配置文本的批量解析与渲染。

让用户可以直接粘贴 / 编辑 dnsmasq 原生配置来管理静态绑定，例如：

    dhcp-host=6E:53:C6:69:A9:DA,set:host_6e53c669a9da,192.168.1.21,fnos,infinite
    dhcp-host=42:F2:2C:11:F9:23,set:host_42f22c11f923,192.168.1.22,win10-edge
    dhcp-host=B0:5C:DA:6A:AD:78,set:host_b05cda6aad78,192.168.1.31,M427FN
    dhcp-option=tag:host_b05cda6aad78,3,192.168.1.254
    dhcp-option=tag:host_b05cda6aad78,6,223.5.5.5,114.114.114.114

支持的写法：
  - MAC 可为 AA:BB:CC:DD:EE:FF / aa-bb-cc-dd-ee-ff / aabb.ccdd.eeff
  - 字段顺序按 dnsmasq 官方语法，IP 与主机名位置可缺省
  - set:标签 可选；配合 dhcp-option=tag:标签 指定网关(3)与 DNS(6)
  - 租期可为 24h / 7d / infinite，写在最后
  - ignore 关键字表示停用该绑定
  - # 开头为注释，空行跳过
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .config import is_valid_ip, is_valid_lease_time, normalize_mac

HOST_PREFIX = "dhcp-host="
OPTION_PREFIX = "dhcp-option="

_MAC_CHARS = re.compile(r"^[0-9A-Fa-f:.\-]+$")

# dhcp-option 的网关 / DNS 代码，同时兼容数字与可读名称
GATEWAY_CODES = {"3", "router", "option:router"}
DNS_CODES = {"6", "dns-server", "option:dns-server", "dns_server", "option:dns_server"}


class ParseError(Exception):
    """单行解析失败，附带原因。"""


def looks_like_mac(segment: str) -> bool:
    """判断一段文本是否是 MAC 地址（兼容 : - . 三种分隔）。"""
    if not segment or not _MAC_CHARS.match(segment):
        return False
    return len(re.sub(r"[^0-9A-Fa-f]", "", segment)) == 12


def static_tag(mac: str) -> str:
    """与 dnsmasq_conf.tag_name_for 保持一致。"""
    return "host_" + normalize_mac(mac).replace(":", "").lower()


def _split_commas(value: str) -> List[str]:
    return [part.strip() for part in value.split(",")]


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def parse_host_line(value: str) -> Dict[str, Any]:
    """解析 dhcp-host= 后的内容，返回条目字典。

    第一个字段必须是 MAC（id: 形式除外），这样报错信息才准确。
    """
    segments = [segment for segment in _split_commas(value) if segment]
    if not segments:
        raise ParseError("内容为空")

    mac = ""
    ip = ""
    name = ""
    lease = ""
    tag = ""
    ignored = False

    start = 0
    if not looks_like_mac(segments[0]):
        # dnsmasq 允许以 id:<client-id> 或 * 开头，此时 MAC 在后一位
        if segments[0].startswith("id:") or segments[0] == "*":
            start = 1
        else:
            raise ParseError("MAC 地址格式不正确：%s" % segments[0])
    if start >= len(segments) or not looks_like_mac(segments[start]):
        raise ParseError("缺少 MAC 地址")
    mac = normalize_mac(segments[start])

    for segment in segments[start + 1:]:
        if not segment:
            continue
        if segment.startswith("set:"):
            tag = segment[4:].strip()
            continue
        if segment.startswith("tag:") or segment.startswith("id:") or segment == "*":
            continue
        if segment == "ignore":
            ignored = True
            continue
        if not ip and is_valid_ip(segment):
            ip = segment
            continue
        if not lease and is_valid_lease_time(segment):
            lease = segment.lower() if segment.lower() == "infinite" else segment
            continue
        if not name:
            name = segment
            continue
        raise ParseError("无法识别的字段：%s" % segment)

    if not ip:
        raise ParseError("缺少 IP 地址")

    return {
        "name": name,
        "mac": mac,
        "ip": ip,
        "gateway": "",
        "dns_servers": [],
        "lease_time": lease,
        "enabled": not ignored,
        "note": "",
        "_tag": tag,
    }


def parse_option_line(value: str) -> Optional[Tuple[str, int, List[str]]]:
    """解析 dhcp-option= 后的内容。

    只处理 tag:xxx 形式且代码为网关(3) / DNS(6) 的行，其余返回 None（忽略）。
    """
    segments = _split_commas(value)
    if len(segments) < 3:
        return None
    target = segments[0]
    if not target.startswith("tag:"):
        return None

    tag = target[4:].strip()
    code_text = segments[1].strip().lower()

    if code_text in GATEWAY_CODES:
        code = 3
    elif code_text in DNS_CODES:
        code = 6
    else:
        return None

    values = [item for item in segments[2:] if item]
    if not values:
        return None
    return tag, code, values


class ParseResult:
    def __init__(self):
        self.entries: List[Dict[str, Any]] = []
        self.errors: List[Dict[str, Any]] = []
        self.ignored: List[Dict[str, Any]] = []
        self.host_lines = 0
        self.option_lines = 0
        self.skipped = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entries": self.entries,
            "errors": self.errors,
            "ignored": self.ignored[:20],
            "stats": {
                "hosts": self.host_lines,
                "options": self.option_lines,
                "skipped": self.skipped,
            },
        }


def parse_config_text(text: str) -> ParseResult:
    """解析一整段 dnsmasq 配置文本。"""
    result = ParseResult()
    entries: List[Tuple[int, Dict[str, Any]]] = []
    tag_options: Dict[str, Dict[int, List[str]]] = {}

    for line_no, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            result.skipped += 1
            continue
        if line.startswith("#") or line.startswith(";"):
            result.skipped += 1
            continue

        lowered = line.lower()
        is_host = lowered.startswith(HOST_PREFIX)
        is_option = lowered.startswith(OPTION_PREFIX)

        if not is_host and not is_option:
            # 从表格复制的裸行（MAC 开头）也按 dhcp-host 解析；
            # 其余不支持的指令静默跳过，避免粘贴整份配置时刷屏报错
            if looks_like_mac(line.split(",")[0].strip()):
                is_host = True
            else:
                result.skipped += 1
                result.ignored.append({"line": line_no, "content": line})
                continue

        if is_host:
            value = line[len(HOST_PREFIX):] if lowered.startswith(HOST_PREFIX) else line
            try:
                entry = parse_host_line(value.strip())
            except ParseError as exc:
                result.errors.append(
                    {"line": line_no, "content": line, "reason": str(exc)}
                )
                continue
            result.host_lines += 1
            entries.append((line_no, entry))
            continue

        value = line[len(OPTION_PREFIX):].strip()
        parsed = parse_option_line(value)
        if parsed is None:
            result.skipped += 1
            result.ignored.append({"line": line_no, "content": line})
            continue
        tag, code, values = parsed
        result.option_lines += 1
        tag_options.setdefault(tag, {})[code] = values

    # 把 dhcp-option 里指定的网关 / DNS 回填到对应条目，并做冲突检测
    seen_mac: Dict[str, int] = {}
    seen_ip: Dict[str, int] = {}

    for line_no, entry in entries:
        tag = entry.pop("_tag", "")
        options = tag_options.get(tag, {}) if tag else {}

        if 3 in options:
            candidate = options[3][0]
            if is_valid_ip(candidate):
                entry["gateway"] = candidate
            else:
                result.errors.append(
                    {"line": line_no, "content": "dhcp-option 3", "reason": "网关地址无效：%s" % candidate}
                )
        if 6 in options:
            dns = [item for item in options[6] if is_valid_ip(item)]
            if dns:
                entry["dns_servers"] = dns

        mac = entry["mac"]
        ip = entry["ip"]
        if mac in seen_mac:
            result.errors.append(
                {
                    "line": line_no,
                    "content": mac,
                    "reason": "MAC 与第 %d 行重复，同一设备只能有一条" % seen_mac[mac],
                }
            )
        else:
            seen_mac[mac] = line_no
        if ip in seen_ip:
            result.errors.append(
                {
                    "line": line_no,
                    "content": ip,
                    "reason": "IP 与第 %d 行的设备重复，同一地址只能分配一次" % seen_ip[ip],
                }
            )
        else:
            seen_ip[ip] = line_no

        result.entries.append(entry)

    return result


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

EXPORT_HEADER = [
    "# dnsmasq 静态绑定：可直接编辑，保存后即时生效",
    "#",
    "# 格式：dhcp-host=MAC,set:标签,IP,主机名[,租期]",
    "#   租期可写 24h / 7d / infinite，留空则使用全局配置",
    "#   主机名可留空：dhcp-host=MAC,set:标签,IP",
    "#",
    "# 单独指定网关或 DNS 时，在下面补两行（标签要与上面的 set: 一致）：",
    "#   dhcp-option=tag:标签,3,192.168.1.254",
    "#   dhcp-option=tag:标签,6,223.5.5.5,114.114.114.114",
    "#",
    "# 以 # 开头的行为注释，不会被解析",
    "",
]


def render_entry(entry: Dict[str, Any]) -> List[str]:
    """把单条静态绑定渲染成 dnsmasq 配置行。"""
    mac = normalize_mac(entry.get("mac", ""))
    tag = static_tag(mac)

    parts = [mac, "set:" + tag, entry.get("ip", "")]
    if entry.get("name"):
        parts.append(entry["name"])
    if entry.get("lease_time"):
        parts.append(entry["lease_time"])
    if entry.get("enabled") is False:
        parts.append("ignore")

    lines = ["dhcp-host=" + ",".join(parts)]
    if entry.get("gateway"):
        lines.append("dhcp-option=tag:%s,3,%s" % (tag, entry["gateway"]))
    dns_servers = [item for item in entry.get("dns_servers", []) if item]
    if dns_servers:
        lines.append("dhcp-option=tag:%s,6,%s" % (tag, ",".join(dns_servers)))
    return lines


def render_static_text(entries: List[Dict[str, Any]], header: bool = True) -> str:
    """把静态绑定列表渲染成可编辑的配置文本。"""
    lines = list(EXPORT_HEADER) if header else []
    for entry in entries:
        lines.extend(render_entry(entry))
    return "\n".join(lines) + "\n" if lines else ""
