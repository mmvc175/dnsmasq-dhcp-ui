"""客户端活跃记录：由 dnsmasq 的 dhcp-script 回调写入，用于判断在线状态。

每条记录形如 { "AA:BB:CC:DD:EE:FF": 1712345678 }，只保留最近 N 天。

注意：dnsmasq 每次 DHCP 事件都会 fork 一个 lease_notify.py 进程，
与主进程并发访问同一个 JSON 文件，因此这里用文件锁 + 原子替换来保证
读到的内容始终是最新的（主进程的内存副本会与磁盘合并，取最新时间戳）。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from typing import Dict, Iterator

from .config import normalize_mac

try:  # pragma: no cover - Windows 等无 fcntl 的平台直接退化为无锁
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

RETENTION_SECONDS = 30 * 24 * 3600


@contextlib.contextmanager
def _file_lock(path: str) -> Iterator[None]:
    """对同名 .lock 文件加排他锁；无 fcntl 时退化为无操作。"""
    if fcntl is None:
        yield
        return
    lock_path = path + ".lock"
    handle = open(lock_path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class ActivityStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: Dict[str, int] = {}
        self._deleted: set = set()
        self._dirty = False

    # -- 读写 -----------------------------------------------------------

    def _read(self) -> Dict[str, int]:
        """读取磁盘上的记录（已过滤过期项）。"""
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return {}
        now = int(time.time())
        return {
            normalize_mac(key): int(value)
            for key, value in raw.items()
            if isinstance(value, (int, float)) and now - int(value) < RETENTION_SECONDS
        }

    def load(self) -> Dict[str, int]:
        with self._lock:
            with _file_lock(self.path):
                self._data = self._read()
            return self._data

    def snapshot(self) -> Dict[str, int]:
        """与磁盘合并后返回（取每个 MAC 的最新时间戳）。

        主进程的 ping 探测写入内存，dnsmasq 回调脚本写入磁盘，
        两边的数据都需要在页面上体现，因此每次取快照时做一次合并。
        """
        with self._lock:
            with _file_lock(self.path):
                external = self._read()
            merged = dict(self._data)
            for mac, timestamp in external.items():
                if mac in self._deleted:
                    continue
                if timestamp > merged.get(mac, 0):
                    merged[mac] = timestamp
            self._data = merged
            return dict(merged)

    def touch(self, mac: str, timestamp: int = None) -> None:
        if not mac:
            return
        key = normalize_mac(mac)
        with self._lock:
            self._data[key] = int(timestamp or time.time())
            self._deleted.discard(key)
            self._dirty = True

    def forget(self, mac: str) -> None:
        """删除记录。用删除集合标记，flush 时才能真正从磁盘移除，
        否则合并逻辑会把磁盘上的旧值重新读回来。"""
        key = normalize_mac(mac)
        with self._lock:
            self._data.pop(key, None)
            self._deleted.add(key)
            self._dirty = True

    def flush(self) -> None:
        """合并磁盘内容后整体写回，避免覆盖其它进程写入的记录。"""
        with self._lock:
            if not self._dirty:
                return
            now = int(time.time())
            with _file_lock(self.path):
                merged = self._read()
                for mac in self._deleted:
                    merged.pop(mac, None)
                for mac, timestamp in self._data.items():
                    if now - timestamp < RETENTION_SECONDS and timestamp > merged.get(mac, 0):
                        merged[mac] = timestamp
                payload = {
                    mac: ts for mac, ts in merged.items() if now - ts < RETENTION_SECONDS
                }
                self._atomic_write(payload)
                self._data = payload
                self._deleted.clear()
            self._dirty = False

    def prune(self) -> None:
        with self._lock:
            now = int(time.time())
            before = len(self._data)
            self._data = {
                mac: ts for mac, ts in self._data.items() if now - ts < RETENTION_SECONDS
            }
            if len(self._data) != before:
                self._dirty = True

    # -- 内部 -----------------------------------------------------------

    def _atomic_write(self, payload: Dict[str, int]) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def handle_dhcp_event(store: ActivityStore, argv: list) -> None:
    """处理 dnsmasq dhcp-script 调用。

    参数： <action> <mac> <ip> <hostname> [<client-id>]
    action: add | old | del
    """
    if len(argv) < 3:
        return
    action = argv[0]
    mac = argv[1]
    if action in ("add", "old"):
        store.touch(mac)
        store.flush()
    elif action == "del":
        store.forget(mac)
        store.flush()
