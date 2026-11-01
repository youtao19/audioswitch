"""守护进程与命令行/GUI 之间的轻量 IPC —— 用 Windows 具名事件。

* quit 事件：让 daemon 优雅退出
* reload 事件：让 daemon 重新读 config.toml（改了配置立即生效，不用重启）
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

QUIT_EVENT = "aswitch-quit-event"
RELOAD_EVENT = "aswitch-reload-event"

EVENT_MODIFY_STATE = 0x0002
INFINITE = 0xFFFFFFFF

kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.SetEvent.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def create(name: str):
    """创建（或打开）一个自动复位的具名事件。"""
    return kernel32.CreateEventW(None, False, False, name)


def wait(handle, timeout: int = INFINITE) -> bool:
    return kernel32.WaitForSingleObject(handle, timeout) == 0


def signal(name: str) -> bool:
    """给正在运行的 daemon 发信号；没有 daemon 时返回 False。"""
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def event_exists(name: str) -> bool:
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def daemon_running() -> bool:
    return event_exists(QUIT_EVENT)
