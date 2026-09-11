"""dnsmasq 子进程管理：启动、停止、热重启、日志环形缓冲、崩溃自愈。

不使用 --no-daemon，改为让 dnsmasq 保持在前台（keep-in-foreground），
由本模块统一收割日志，异常退出时自动拉起。
"""

from __future__ import annotations

import signal
import subprocess
import threading
import time
from collections import deque
from typing import Deque, List, Optional

from . import dnsmasq_conf

LOG_BUFFER_SIZE = 800


class DnsmasqProcess:
    def __init__(self, binary: str = None):
        self.binary = binary or dnsmasq_conf.DNSMASQ_BIN
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._monitor: Optional[threading.Thread] = None
        self._logs: Deque[str] = deque(maxlen=LOG_BUFFER_SIZE)
        self.restart_count = 0
        self.last_exit: Optional[int] = None
        self.last_started: Optional[float] = None

    # -- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return
            self._stopping.clear()
            self._proc = subprocess.Popen(
                [self.binary, "--conf-file=" + dnsmasq_conf.MAIN_CONF, "--keep-in-foreground"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            self.last_started = time.time()
            self._append_log("[supervisor] dnsmasq 启动，pid=%d" % self._proc.pid)

            self._reader = threading.Thread(target=self._pump_output, daemon=True)
            self._reader.start()

            if self._monitor is None or not self._monitor.is_alive():
                self._monitor = threading.Thread(target=self._watch, daemon=True)
                self._monitor.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._stopping.set()
            if not self._proc:
                return
            proc = self._proc
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._proc = None
            self._append_log("[supervisor] dnsmasq 已停止")

    def restart(self) -> None:
        """重新加载配置：优先 SIGHUP，失败则整体重启。"""
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.send_signal(signal.SIGHUP)
                    self._append_log("[supervisor] 已发送 SIGHUP 重新加载配置")
                    return
                except Exception:
                    pass
            self.stop()
            self.start()

    def hard_restart(self) -> None:
        """强制重启（配置结构性变化时使用）。"""
        self.stop()
        self.start()

    # -- 状态 -----------------------------------------------------------

    @property
    def running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    def status(self) -> dict:
        uptime = 0
        if self.running and self.last_started:
            uptime = int(time.time() - self.last_started)
        return {
            "running": self.running,
            "pid": self.pid,
            "uptime": uptime,
            "restartCount": self.restart_count,
            "lastExit": self.last_exit,
        }

    def logs(self, limit: int = 200) -> List[str]:
        with self._lock:
            items = list(self._logs)
        return items[-limit:] if limit > 0 else items

    # -- 内部 -----------------------------------------------------------

    def _append_log(self, line: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self._logs.append("[%s] %s" % (stamp, line.rstrip()))

    def _pump_output(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                with self._lock:
                    self._append_log(line.rstrip())
        except Exception:
            pass

    def _watch(self) -> None:
        """进程异常退出时自动拉起。"""
        backoff = 1.0
        while not self._stopping.is_set():
            proc = self._proc
            if proc is None:
                time.sleep(1.0)
                continue
            if proc.poll() is None:
                time.sleep(2.0)
                continue
            self.last_exit = proc.returncode
            with self._lock:
                self._append_log(
                    "[supervisor] dnsmasq 退出，code=%s，%.1fs 后重启" % (proc.returncode, backoff)
                )
            time.sleep(backoff)
            if self._stopping.is_set():
                break
            backoff = min(backoff * 2, 30.0)
            try:
                self.restart_count += 1
                self.start()
                backoff = 1.0
            except Exception as exc:
                self._append_log("[supervisor] 重启失败：%s" % exc)
