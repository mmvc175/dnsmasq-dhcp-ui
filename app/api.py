"""HTTP 接口层：JSON API + 静态资源 + 可选 Basic Auth。"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import bulk, dnsmasq_conf, leases as leases_mod, probe
from .config import (
    ValidationError,
    is_valid_mac,
    normalize_mac,
    validate_dhcp_block,
    validate_dns_block,
    validate_static_entry,
)

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
HOSTS_FILE = os.environ.get("HOSTS_FILE", "/data/hosts.dnsmasq")
LEASE_SCRIPT = os.environ.get("LEASE_SCRIPT", "/app/scripts/lease_notify.py")

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class ApiContext:
    """API 所需依赖的集合，由 main.py 注入。"""

    def __init__(self, app_config, dnsmasq, activity, lease_file: str, version: str):
        self.app_config = app_config
        self.dnsmasq = dnsmasq
        self.activity = activity
        self.lease_file = lease_file
        self.version = version
        self.started_at = time.time()

    # -- 便捷方法 -------------------------------------------------------

    def reload(self, hard: bool = False) -> None:
        """重新加载 dnsmasq（先自检配置）。"""
        ok, output = dnsmasq_conf.test_config()
        if not ok:
            raise ValidationError("配置校验未通过：%s" % output)
        if hard:
            self.dnsmasq.hard_restart()
        else:
            self.dnsmasq.restart()

    def commit(self, mutator) -> Dict[str, Any]:
        """原子地应用一次配置变更：

        修改 → 落盘 → 渲染 dnsmasq 配置 → dnsmasq --test 自检 → 重启服务。
        任一步失败都回滚到变更前的配置，避免出现"配置已保存但服务没生效"。
        """
        previous = self.app_config.snapshot()
        try:
            self.app_config.update(mutator)
            dnsmasq_conf.apply(
                self.app_config.snapshot(),
                lease_file=self.lease_file,
                hosts_file=HOSTS_FILE,
                script=LEASE_SCRIPT,
            )
            ok, output = dnsmasq_conf.test_config()
            if not ok:
                raise ValidationError("dnsmasq 配置自检未通过：%s" % output)
            self.dnsmasq.hard_restart()
        except Exception:
            self._rollback(previous)
            raise
        return self.app_config.snapshot()

    def _rollback(self, previous: Dict[str, Any]) -> None:
        """恢复配置并重新渲染；回滚失败不能掩盖原始异常。"""
        try:
            def restore(data):
                data.clear()
                data.update(json.loads(json.dumps(previous)))

            self.app_config.update(restore)
            dnsmasq_conf.apply(
                self.app_config.snapshot(),
                lease_file=self.lease_file,
                hosts_file=HOSTS_FILE,
                script=LEASE_SCRIPT,
            )
        except Exception as exc:
            print("[api] 回滚失败：%s" % exc)

    def lease_time_seconds(self) -> Optional[int]:
        text = str(self.app_config.snapshot()["dhcp"].get("lease_time", "24h")).strip().lower()
        if text == "infinite":
            return None
        match = re.match(r"^(\d+)([smhdw]?)$", text)
        if not match:
            return None
        value = int(match.group(1))
        unit = match.group(2)
        return value * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]

    def lease_rows(self) -> list:
        config = self.app_config.snapshot()
        return leases_mod.build_leases(
            lease_file=self.lease_file,
            static_leases=config.get("static_leases", []),
            activity=self.activity.snapshot(),
            online_window=int(config.get("ui", {}).get("online_window", 900)),
            lease_time_seconds=self.lease_time_seconds(),
        )

    def state(self) -> Dict[str, Any]:
        config = self.app_config.snapshot()
        rows = self.lease_rows()
        return {
            "config": config,
            "leases": rows,
            "summary": leases_mod.summarize(rows),
            "service": self.dnsmasq.status(),
            "meta": {
                "version": self.version,
                "uptime": int(time.time() - self.started_at),
                "leaseFile": self.lease_file,
                "authRequired": bool(config.get("auth", {}).get("enabled")),
            },
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "dnsmasq-dhcp-ui"
    ctx: ApiContext = None  # 由 make_server 注入

    # -- 基础 -----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get("DEBUG"):
            print("[http] %s %s" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str, extra: dict = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _ok(self, message: str = "ok", data: Any = None) -> None:
        self._send_json(200, {"ok": True, "message": message, "data": data})

    def _fail(self, code: int, message: str, detail: str = "") -> None:
        payload = {"ok": False, "message": message}
        if detail:
            payload["detail"] = detail
        self._send_json(code, payload)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if "json" in content_type:
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                raise ValidationError("请求体不是合法 JSON")
        parsed = parse_qs(raw.decode("utf-8"))
        return {key: values[0] for key, values in parsed.items()}

    # -- 认证 -----------------------------------------------------------

    def _authorized(self) -> bool:
        auth = self.ctx.app_config.snapshot().get("auth", {})
        if not auth.get("enabled"):
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:
            return False
        user, _, passwd = decoded.partition(":")
        return user == auth.get("username", "") and passwd == auth.get("password", "")

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        body = json.dumps(
            {"ok": False, "message": "需要认证", "detail": "请输入正确的用户名与密码"},
            ensure_ascii=False,
        ).encode("utf-8")
        self._send(
            401,
            body,
            "application/json; charset=utf-8",
            extra={"WWW-Authenticate": 'Basic realm="dnsmasq-dhcp-ui"'},
        )
        return False

    # -- 路由 -----------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            if not self._require_auth():
                return
            self._route_api(path, parsed.query)
            return
        self._serve_static(path)

    def do_HEAD(self) -> None:
        self._serve_static(urlparse(self.path).path)

    def do_POST(self) -> None:
        self._mutate("POST")

    def do_PUT(self) -> None:
        self._mutate("PUT")

    def do_DELETE(self) -> None:
        self._mutate("DELETE")

    def _mutate(self, method: str) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self._fail(404, "接口不存在")
            return
        if not self._require_auth():
            return
        self._route_api(path, urlparse(self.path).query, body=self._body())

    def _route_api(self, path: str, query: str, body: Dict[str, Any] = None) -> None:
        body = body or {}
        params = parse_qs(query)
        routes: Dict[str, Tuple[Dict[str, Callable], Dict[str, Callable]]] = {
            "/api/state": ({"GET": self._get_state}, {}),
            "/api/leases": ({"GET": self._get_leases}, {}),
            "/api/config": ({"GET": self._get_config}, {"POST": self._save_config, "PUT": self._save_config}),
            "/api/static": ({"GET": self._get_static}, {"POST": self._save_static}),
            "/api/static/delete": ({}, {"POST": self._delete_static, "DELETE": self._delete_static}),
            "/api/static/export": ({"GET": self._export_static}, {}),
            "/api/static/preview": ({}, {"POST": self._preview_static}),
            "/api/static/import": ({}, {"POST": self._import_static}),
            "/api/service/reload": ({}, {"POST": self._reload_service}),
            "/api/logs": ({"GET": self._get_logs}, {}),
            "/api/probe": ({}, {"POST": self._probe}),
            "/api/raw": ({"GET": self._get_raw_config}, {}),
        }

        if path not in routes:
            self._fail(404, "接口不存在：%s" % path)
            return

        get_map, post_map = routes[path]
        method = self.command
        if method == "GET" and method in get_map:
            handler = get_map[method]
        elif method in post_map:
            handler = post_map[method]
        else:
            self._fail(405, "方法不被支持")
            return

        try:
            handler(body, params)
        except ValidationError as exc:
            self._fail(400, str(exc))
        except FileNotFoundError as exc:
            self._fail(404, str(exc))
        except Exception as exc:
            self._fail(500, "服务器内部错误", str(exc))

    # -- 接口实现 -------------------------------------------------------

    def _get_state(self, body, params) -> None:
        self._ok("获取成功", self.ctx.state())

    def _get_leases(self, body, params) -> None:
        rows = self.ctx.lease_rows()
        self._ok("获取成功", {"leases": rows, "summary": leases_mod.summarize(rows)})

    def _get_config(self, body, params) -> None:
        self._ok("获取成功", self.ctx.app_config.snapshot())

    def _save_config(self, body, params) -> None:
        """支持只提交 dhcp 或只提交 dns，未提交的段落保持原值。"""
        ctx = self.ctx
        has_dhcp = body.get("dhcp") is not None
        has_dns = body.get("dns") is not None
        if not has_dhcp and not has_dns:
            raise ValidationError("缺少要保存的配置")

        dhcp = validate_dhcp_block(body["dhcp"]) if has_dhcp else None
        dns = validate_dns_block(body["dns"]) if has_dns else None

        def mutate(data):
            if dhcp is not None:
                data["dhcp"] = dhcp
            if dns is not None:
                data["dns"] = dns
            window = (body.get("ui") or {}).get("online_window")
            if window is not None:
                data["ui"]["online_window"] = max(30, int(window))

        data = ctx.commit(mutate)
        self._ok("配置已保存并生效", data)

    def _get_static(self, body, params) -> None:
        self._ok("获取成功", self.ctx.app_config.snapshot().get("static_leases", []))

    def _save_static(self, body, params) -> None:
        ctx = self.ctx
        entry = validate_static_entry(body)

        def mutate(data):
            items = data.setdefault("static_leases", [])
            for index, item in enumerate(items):
                if normalize_mac(item.get("mac", "")) == entry["mac"]:
                    items[index] = entry
                    return
            for index, item in enumerate(items):
                if item.get("ip") == entry["ip"] and normalize_mac(item.get("mac", "")) != entry["mac"]:
                    raise ValidationError("IP %s 已被 %s 占用" % (entry["ip"], item.get("mac")))
            items.append(entry)

        ctx.commit(mutate)
        self._ok("静态绑定已保存", entry)

    def _delete_static(self, body, params) -> None:
        ctx = self.ctx
        mac = normalize_mac(
            (body.get("mac") or (params.get("mac") or [""])[0] or "").strip()
        )
        if not is_valid_mac(mac):
            raise ValidationError("MAC 格式不正确")

        exists = any(
            normalize_mac(i.get("mac", "")) == mac
            for i in ctx.app_config.snapshot().get("static_leases", [])
        )
        if not exists:
            raise ValidationError("未找到该 MAC 的静态绑定")

        def mutate(data):
            items = data.get("static_leases", [])
            for index, item in enumerate(items):
                if normalize_mac(item.get("mac", "")) == mac:
                    items.pop(index)
                    break

        ctx.commit(mutate)
        self._ok("静态绑定已删除", {"mac": mac})

    # -- 批量导入 / 导出 -------------------------------------------------

    @staticmethod
    def _active_static(data) -> list:
        return [item for item in data.get("static_leases", []) if item.get("enabled", True)]

    def _export_static(self, body, params) -> None:
        """导出当前静态绑定为 dnsmasq 配置文本（停用的条目不导出）。"""
        entries = self._active_static(self.ctx.app_config.snapshot())
        self._ok("导出成功", {
            "text": bulk.render_static_text(entries),
            "count": len(entries),
        })

    def _find_conflicts(self, entries, mode) -> list:
        """新条目与现有配置之间的 IP 冲突。"""
        existing = self.ctx.app_config.snapshot().get("static_leases", [])
        incoming_macs = {entry["mac"] for entry in entries}
        conflicts = []
        for entry in entries:
            for item in existing:
                mac = normalize_mac(item.get("mac", ""))
                if mac == entry["mac"]:
                    continue
                if mode == "replace" and mac in incoming_macs:
                    continue  # 这条现有记录会被替换掉
                if item.get("ip") == entry["ip"]:
                    conflicts.append({
                        "ip": entry["ip"],
                        "mac": entry["mac"],
                        "name": entry.get("name", ""),
                        "existingMac": mac,
                        "existingName": item.get("name", ""),
                    })
        return conflicts

    def _preview_static(self, body, params) -> None:
        """解析配置文本但不保存，供界面实时预览。"""
        text = body.get("text") or ""
        mode = body.get("mode", "merge")
        result = bulk.parse_config_text(text)
        conflicts = self._find_conflicts(result.entries, mode)
        self._ok("解析完成", {
            "entries": result.entries,
            "errors": result.errors,
            "ignored": result.ignored[:20],
            "stats": result.to_dict()["stats"],
            "conflicts": conflicts,
            "valid": result.ok and not conflicts,
        })

    def _import_static(self, body, params) -> None:
        """批量导入。mode: merge（按 MAC 更新或新增） / replace（整体替换）。"""
        ctx = self.ctx
        text = body.get("text") or ""
        mode = (body.get("mode") or "merge").strip()
        if mode not in ("merge", "replace"):
            raise ValidationError("导入模式不正确")

        result = bulk.parse_config_text(text)
        if result.errors:
            detail = "\n".join(
                "第 %s 行 %s：%s" % (err["line"], err.get("content", ""), err["reason"])
                for err in result.errors[:12]
            )
            self._fail(400, "配置中有 %d 处错误，请修正后再保存" % len(result.errors), detail)
            return

        conflicts = self._find_conflicts(result.entries, mode)
        if conflicts:
            detail = "\n".join(
                "%s 已被 %s（%s）占用" % (c["ip"], c["existingMac"], c["existingName"] or "未命名")
                for c in conflicts[:12]
            )
            self._fail(400, "有 %d 个 IP 与其它绑定冲突" % len(conflicts), detail)
            return

        if not result.entries:
            raise ValidationError("没有解析到任何静态绑定")

        entries = [validate_static_entry(entry) for entry in result.entries]

        def mutate(data):
            items = data.setdefault("static_leases", [])
            if mode == "replace":
                # 停用的条目保留，其余整体替换，避免手工停用的记录被清掉
                kept = [item for item in items if item.get("enabled") is False]
                items[:] = kept + entries
                return
            for entry in entries:
                for index, item in enumerate(items):
                    if normalize_mac(item.get("mac", "")) == entry["mac"]:
                        items[index] = entry
                        break
                else:
                    items.append(entry)

        ctx.commit(mutate)
        self._ok(
            "已导入 %d 条静态绑定" % len(entries),
            {
                "count": len(entries),
                "mode": mode,
                "ignored": len(result.ignored),
                "total": len(ctx.app_config.snapshot().get("static_leases", [])),
            },
        )

    def _reload_service(self, body, params) -> None:
        hard = str(body.get("hard", "1")) in ("1", "true", "yes")
        self.ctx.reload(hard=hard)
        self._ok("服务已重新加载", self.ctx.dnsmasq.status())

    def _get_logs(self, body, params) -> None:
        limit = int((params.get("limit") or ["200"])[0] or 200)
        self._ok("获取成功", {"logs": self.ctx.dnsmasq.logs(limit)})

    def _probe(self, body, params) -> None:
        """主动 ping 探测当前所有已知 IP。"""
        rows = self.ctx.lease_rows()
        ips = [row["ip"] for row in rows if row.get("ip")]
        results = probe.sweep(ips)
        now = int(time.time())
        for row in rows:
            if results.get(row["ip"]):
                self.ctx.activity.touch(row["mac"], now)
        self.ctx.activity.flush()
        self._ok("探测完成", {"checked": len(ips), "alive": sum(1 for v in results.values() if v)})

    def _get_raw_config(self, body, params) -> None:
        managed = ""
        if os.path.exists(dnsmasq_conf.MANAGED_CONF):
            with open(dnsmasq_conf.MANAGED_CONF, "r", encoding="utf-8") as handle:
                managed = handle.read()
        main = ""
        if os.path.exists(dnsmasq_conf.MAIN_CONF):
            with open(dnsmasq_conf.MAIN_CONF, "r", encoding="utf-8") as handle:
                main = handle.read()
        self._ok("获取成功", {"main": main, "managed": managed})

    # -- 静态资源 -------------------------------------------------------

    def _serve_static(self, path: str) -> None:
        if path in ("", "/", "/index.html"):
            target = os.path.join(WEB_DIR, "index.html")
        else:
            relative = path.lstrip("/")
            target = os.path.abspath(os.path.join(WEB_DIR, relative))
            if not target.startswith(os.path.abspath(WEB_DIR)):
                self._fail(403, "禁止访问")
                return
        if not os.path.isfile(target):
            self._send(404, b"Not Found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as handle:
            content = handle.read()
        self._send(200, content, STATIC_TYPES.get(ext, "application/octet-stream"))


def make_server(ctx: ApiContext, host: str, port: int) -> ThreadingHTTPServer:
    handler_cls = type("BoundHandler", (Handler,), {"ctx": ctx})
    server = ThreadingHTTPServer((host, port), handler_cls)
    server.daemon_threads = True
    return server
