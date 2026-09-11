"""把应用配置渲染成 dnsmasq 配置，并做配置语法自检。

生成的目标文件是 /etc/dnsmasq.d/10-managed.conf，
主配置 /etc/dnsmasq.conf 只包含骨架（租约文件、日志、conf-dir 等）。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any, Dict, List, Tuple

from .config import normalize_mac

MAIN_CONF = "/etc/dnsmasq.conf"
MANAGED_CONF = "/etc/dnsmasq.d/10-managed.conf"
DNSMASQ_BIN = os.environ.get("DNSMASQ_BIN", "dnsmasq")

MAIN_CONF_TEMPLATE = """# 由 dnsmasq-dhcp-ui 生成，请勿手工修改本文件。
# 动态调整的部分位于 /etc/dnsmasq.d/10-managed.conf

conf-dir=/etc/dnsmasq.d/,*.conf

# 进程与日志
user=root
keep-in-foreground
log-facility=-
log-dhcp

# 角色（port=0 表示关闭 DNS 服务，仅提供 DHCP）
{dns_port}
domain-needed
bogus-priv
expand-hosts
cache-size=1500
dns-forward-max=1500

# 主机记录
addn-hosts={hosts_file}

# 租约
dhcp-leasefile={lease_file}
dhcp-script={script}
dhcp-scriptuser=root

dhcp-lease-max=1000
"""

TAG_PREFIX = "host_"


def tag_name_for(mac: str) -> str:
    """由 MAC 生成稳定的 tag 名，用于下发 per-host 网关 / DNS。"""
    return TAG_PREFIX + normalize_mac(mac).replace(":", "").lower()


def render_managed(config: Dict[str, Any]) -> str:
    """渲染受管理的配置片段。"""
    dhcp: Dict[str, Any] = config.get("dhcp", {})
    dns: Dict[str, Any] = config.get("dns", {})
    lines: List[str] = [
        "# 由 dnsmasq-dhcp-ui 自动生成，手工修改会在下次保存时被覆盖。",
        "",
    ]

    # ---------------- DNS 部分 ----------------
    if dns.get("enabled", True):
        if dns.get("no_resolv", True):
            lines.append("no-resolv")
        forwarders = [f for f in dns.get("forwarders", []) if f]
        if forwarders:
            for item in forwarders:
                lines.append("server=%s" % item)
        else:
            lines.append("# 未配置上游 DNS，dnsmasq 将回退使用 /etc/resolv.conf")
    else:
        lines.append("# DNS 服务已关闭，仅提供 DHCP")
    lines.append("")

    # ---------------- DHCP 部分 ----------------
    if not dhcp.get("enabled", True):
        lines.append("# DHCP 服务已关闭：不输出 dhcp-range，dnsmasq 将不响应 DHCP 请求")
        lines.append("")
        _append_static_section(lines, config)
        _append_custom_section(lines, dhcp)
        return "\n".join(lines) + "\n"

    interface = (dhcp.get("interface") or "").strip()
    if interface:
        for name in interface.split(","):
            name = name.strip()
            if name:
                lines.append("interface=%s" % name)
        lines.append("bind-dynamic")
    else:
        lines.append("bind-dynamic")
        lines.append("except-interface=lo")

    netmask = (dhcp.get("netmask") or "").strip()
    lease_time = (dhcp.get("lease_time") or "24h").strip()
    range_line = "dhcp-range=%s,%s" % (dhcp.get("range_start"), dhcp.get("range_end"))
    if netmask:
        range_line += "," + netmask
    range_line += "," + lease_time
    lines.append(range_line)

    if dhcp.get("authoritative", True):
        lines.append("dhcp-authoritative")

    domain = (dhcp.get("domain") or "").strip()
    if domain:
        lines.append("domain=%s" % domain)

    gateway = (dhcp.get("gateway") or "").strip()
    if gateway:
        lines.append("dhcp-option=3,%s" % gateway)

    dns_servers = [d for d in dhcp.get("dns_servers", []) if d]
    if dns_servers:
        lines.append("dhcp-option=6,%s" % ",".join(dns_servers))

    lines.append("")
    _append_static_section(lines, config)
    _append_custom_section(lines, dhcp)
    return "\n".join(lines) + "\n"


def _append_custom_section(lines: List[str], dhcp: Dict[str, Any]) -> None:
    """追加用户自定义的 dnsmasq 指令。"""
    custom = (dhcp.get("custom_options") or "").strip()
    if not custom:
        return
    lines.append("# ---- 自定义配置（在界面的 DHCP 配置页维护）----")
    for raw in custom.splitlines():
        stripped = raw.strip()
        if stripped:
            lines.append(stripped)
    lines.append("")


def _append_static_section(lines: List[str], config: Dict[str, Any]) -> None:
    lines.append("# ---- 静态绑定 ----")
    for entry in config.get("static_leases", []):
        if not entry.get("enabled", True):
            lines.append("# 已停用：%s %s" % (entry.get("mac"), entry.get("ip")))
            continue

        mac = normalize_mac(entry.get("mac", ""))
        ip = entry.get("ip", "").strip()
        name = (entry.get("name") or "").strip()
        lease_time = (entry.get("lease_time") or "").strip()
        gateway = (entry.get("gateway") or "").strip()
        dns_servers = [d for d in entry.get("dns_servers", []) if d]

        host_line = "dhcp-host=%s,set:%s,%s" % (mac, tag_name_for(mac), ip)
        if name:
            host_line += "," + name
        if lease_time:
            host_line += "," + lease_time
        lines.append(host_line)

        if gateway:
            lines.append("dhcp-option=tag:%s,3,%s" % (tag_name_for(mac), gateway))
        if dns_servers:
            lines.append(
                "dhcp-option=tag:%s,6,%s" % (tag_name_for(mac), ",".join(dns_servers))
            )
    lines.append("")


def render_hosts(config: Dict[str, Any]) -> str:
    """生成 hosts 文件，让静态绑定的设备名可被本机解析。"""
    rows: List[str] = ["# 由 dnsmasq-dhcp-ui 生成：静态绑定主机记录"]
    for entry in config.get("static_leases", []):
        if not entry.get("enabled", True):
            continue
        name = (entry.get("name") or "").strip()
        ip = (entry.get("ip") or "").strip()
        if name and ip:
            rows.append("%s\t%s" % (ip, name))
    domain = (config.get("dhcp", {}).get("domain") or "").strip()
    if domain:
        extra = []
        for entry in config.get("static_leases", []):
            if not entry.get("enabled", True):
                continue
            name = (entry.get("name") or "").strip()
            ip = (entry.get("ip") or "").strip()
            if name and ip:
                extra.append("%s\t%s.%s" % (ip, name, domain))
        rows.extend(extra)
    return "\n".join(rows) + "\n"


def atomic_write(path: str, content: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_main_conf(lease_file: str, hosts_file: str, script: str, dns_enabled: bool) -> None:
    content = MAIN_CONF_TEMPLATE.format(
        lease_file=lease_file,
        hosts_file=hosts_file,
        script=script,
        dns_port="port=53" if dns_enabled else "port=0",
    )
    atomic_write(MAIN_CONF, content)


def apply(config: Dict[str, Any], lease_file: str, hosts_file: str, script: str) -> str:
    """渲染并写入全部配置文件，返回受管理配置的内容。"""
    dns_enabled = bool(config.get("dns", {}).get("enabled", True))
    write_main_conf(lease_file, hosts_file, script, dns_enabled)
    managed = render_managed(config)
    atomic_write(MANAGED_CONF, managed)
    atomic_write(hosts_file, render_hosts(config))
    return managed


def test_config() -> Tuple[bool, str]:
    """用 dnsmasq --test 校验配置，返回 (是否通过, 输出)。"""
    if not os.path.exists(DNSMASQ_BIN) and _which(DNSMASQ_BIN) is None:
        return True, "dnsmasq 不可用，跳过语法检查"
    try:
        proc = subprocess.run(
            [DNSMASQ_BIN, "--conf-file=%s" % MAIN_CONF, "--test"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return True, "dnsmasq 不可用，跳过语法检查"
    except subprocess.TimeoutExpired:
        return False, "配置检查超时"
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output.strip()


def _which(name: str):
    for path in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(path, name)
        if os.path.exists(candidate):
            return candidate
    return None
