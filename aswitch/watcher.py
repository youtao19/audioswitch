"""进程监听：规则命中 -> 切 profile；规则进程全部退出 -> 还原。"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes

from . import engine
from .config import Config

log = logging.getLogger("aswitch")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def process_map() -> dict[int, str]:
    """pid -> 可执行文件名（小写）。用 Toolhelp，无需 psutil。"""
    result: dict[int, str] = {}
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return result
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile:
                result[int(entry.th32ProcessID)] = entry.szExeFile.lower()
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return result


def running_exes() -> set[str]:
    """当前所有进程的可执行文件名（小写）。"""
    return set(process_map().values())


class RuleWatcher(threading.Thread):
    """后台线程：定时看进程列表，自动切设备 / 还原。

    状态机：
      * 首次命中规则 -> 记录当前设备快照，切到规则 profile
      * 命中另外的规则 -> 直接切到新 profile（不还原）
      * 一条规则都不命中 -> 等 restore_delay 秒后按快照还原
    """

    def __init__(self, cfg: Config, *, notify=None, dry_run: bool = False,
                 on_state=None) -> None:
        super().__init__(daemon=True, name="aswitch-watcher")
        self.cfg = cfg
        self._notify = notify or (lambda *a, **k: None)
        self._on_state = on_state or (lambda *a, **k: None)
        self.dry_run = dry_run
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.active_profile: str | None = None
        self._saved: dict[tuple[int, int], str] | None = None
        self._pending_restore_at: float | None = None
        self.last_match: str | None = None
        self._warned: set[str] = set()

    # ---------- 外部控制 ----------
    def stop(self) -> None:
        self._stop_event.set()

    def adopt(self, profile: str | None, saved: dict[tuple[int, int], str] | None) -> None:
        """外部已经手动切好了：直接接管状态。

        用于“一键收录”：收录流程自己先快照旧设备、再切新设备，所以不能让
        watcher 把这个已切好的状态当成“进入前的快照”，否则退出时会还原错。
        """
        with self._lock:
            self.active_profile = profile
            self._saved = saved
            self._pending_restore_at = None

    # ---------- 规则判定 ----------
    def decide(self, exes: set[str]) -> tuple[str, str] | None:
        """返回 (profile, 命中的 exe)；按配置顺序，第一条命中的规则生效。"""
        for rule in self.cfg.rules:
            if not rule.enabled:
                continue
            hit = rule.matches(exes)
            if hit:
                return rule.profile, hit
        return None

    # ---------- 主循环 ----------
    def run(self) -> None:
        interval = float(self.cfg.setting("poll_interval") or 1.0)
        rules = [r for r in self.cfg.rules if r.enabled]
        log.info("进程监听启动：%d 条规则，间隔 %.1fs", len(rules), interval)
        for rule in rules:
            log.info("  规则 %s -> %s", ", ".join(rule.exe), rule.profile)
        while not self._stop_event.is_set():
            try:
                self.tick(running_exes())
            except Exception:  # noqa: BLE001
                log.exception("watcher tick 异常")
            self._stop_event.wait(interval)

    def tick(self, exes: set[str]) -> None:
        decision = self.decide(exes)
        with self._lock:
            now = time.monotonic()
            if decision is None:
                self.last_match = None
                if self.active_profile is None:
                    self._pending_restore_at = None
                    return
                delay = float(self.cfg.setting("restore_delay") or 0)
                if self._pending_restore_at is None:
                    self._pending_restore_at = now + delay
                    log.info("规则进程已退出，%.0fs 后还原设备", delay)
                if now >= self._pending_restore_at:
                    self._restore()
                return

            self._pending_restore_at = None
            profile, hit = decision
            self.last_match = hit
            if profile == self.active_profile:
                return
            if self.active_profile is None:
                self._saved = engine.snapshot()
            self._apply(profile, hit)

    # ---------- 动作 ----------
    def _apply(self, profile: str, hit: str) -> None:
        if self.dry_run:
            log.info("[dry-run] %s -> 切换到 %s", hit, profile)
            self.active_profile = profile
            return
        try:
            res = engine.apply_profile(self.cfg, profile)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if msg not in self._warned:
                self._warned.add(msg)
                log.warning("自动切换到 %s 失败: %s", profile, msg)
                self._notify("自动切换失败", msg, warning=True)
            self.active_profile = profile
            return
        self._warned.clear()
        self.active_profile = profile
        log.info("检测到 %s -> 已切换到 %s (%s)", hit, profile, res.render)
        if res.changed:
            self._notify(f"已切换到 {profile}", res.render)
        self._on_state(self.active_profile, self._saved)

    def _restore(self) -> None:
        saved = self._saved
        prev = self.active_profile
        self.active_profile = None
        self._saved = None
        self._pending_restore_at = None
        if not saved:
            return
        if self.dry_run:
            log.info("[dry-run] 还原设备")
            return
        try:
            warnings = engine.restore(saved)
            for w in warnings:
                log.debug("还原: %s", w)
            log.info("规则结束，已还原设备（离开 %s）", prev)
            self._notify("已还原设备", "游戏已退出")
        except Exception as exc:  # noqa: BLE001
            log.warning("还原设备失败: %s", exc)
        self._on_state(None, None)
