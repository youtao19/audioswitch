"""全局热键：RegisterHotKey + 消息循环。

用 RegisterHotKey 而不是键盘钩子（WH_KEYBOARD_LL）：
系统级注册，不注入任何进程，不会被反作弊（Vanguard / EAC）盯上。
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import byref, wintypes

log = logging.getLogger("aswitch")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_APP_CALLBACK = 0x8000 + 42  # 自定义：把回调甩回消息循环线程执行

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL

_MODS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
}

_KEYS = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B,
    "escape": 0x1B, "backspace": 0x08, "insert": 0x2D, "ins": 0x2D,
    "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "minus": 0xBD, "-": 0xBD, "plus": 0xBB, "=": 0xBB,
    ",": 0xBC, ".": 0xBE, "/": 0xBF, ";": 0xBA, "'": 0xDE,
    "[": 0xDB, "]": 0xDD, "\\": 0xDC, "`": 0xC0,
}
for _i in range(1, 25):
    _KEYS[f"f{_i}"] = 0x6F + _i
for _c in "0123456789":
    _KEYS[_c] = 0x30 + int(_c)
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _KEYS[_c.lower()] = ord(_c)


class HotkeyError(RuntimeError):
    pass


def parse(spec: str) -> tuple[int, int]:
    """'ctrl+alt+f1' -> (modifiers, virtual_key)"""
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError(f"无效热键: {spec!r}")
    mods = 0
    vk = None
    for part in parts:
        if part in _MODS:
            mods |= _MODS[part]
        elif part in _KEYS:
            if vk is not None:
                raise HotkeyError(f"热键 {spec!r} 里有两个主键")
            vk = _KEYS[part]
        else:
            raise HotkeyError(f"热键 {spec!r} 里不认识的键: {part}")
    if vk is None:
        raise HotkeyError(f"热键 {spec!r} 缺少主键（如 f1 / 1 / a）")
    return mods, vk


class HotkeyManager:
    """在主线程调用 run()，它会阻塞并派发热键回调。"""

    def __init__(self, bindings: dict[str, object]) -> None:
        self._specs = bindings
        self._callbacks: dict[int, tuple[str, object]] = {}
        self._thread_id = 0
        self._next_id = 1
        self._pending: list = []

    def register(self) -> None:
        for spec, callback in self._specs.items():
            try:
                mods, vk = parse(spec)
            except HotkeyError as exc:
                log.warning("%s", exc)
                continue
            hk_id = self._next_id
            if not user32.RegisterHotKey(None, hk_id, mods | MOD_NOREPEAT, vk):
                err = ctypes.get_last_error()
                if err == 1409:
                    log.warning(
                        "热键 %s 已被其它程序占用（1409）。常见占用方：NVIDIA App 的"
                        "截图/录制快捷键（Alt+F1、Alt+F9、Alt+F10）。在 NVIDIA App → 设置 → "
                        "快捷键里改掉，或在本工具里换成别的组合。",
                        spec,
                    )
                else:
                    log.warning("注册热键 %s 失败，错误码 %d", spec, err)
                continue
            self._callbacks[hk_id] = (spec, callback)
            self._next_id += 1
            log.info("已注册热键 %s -> %s", spec, getattr(callback, "__name__", callback))

    def unregister(self) -> None:
        for hk_id in self._callbacks:
            user32.UnregisterHotKey(None, hk_id)
        self._callbacks.clear()

    def reload(self, bindings: dict[str, object]) -> None:
        """换一组热键绑定并重新注册。"""
        self.unregister()
        self._specs = bindings
        self.register()

    def run(self) -> None:
        """阻塞的消息循环（同时负责派发同线程窗口消息，例如托盘菜单）。"""
        self._thread_id = kernel32.GetCurrentThreadId()
        self.register()
        if not self._callbacks:
            log.warning("没有任何热键注册成功")
        msg = wintypes.MSG()
        try:
            while True:
                ret = user32.GetMessageW(byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break
                if msg.message == WM_HOTKEY:
                    entry = self._callbacks.get(int(msg.wParam))
                    if entry:
                        spec, callback = entry
                        try:
                            callback()  # type: ignore[operator]
                        except Exception:  # noqa: BLE001
                            log.exception("热键 %s 的处理函数出错", spec)
                elif msg.message == WM_APP_CALLBACK:
                    self._drain()
                else:
                    user32.TranslateMessage(byref(msg))
                    user32.DispatchMessageW(byref(msg))
        finally:
            self.unregister()

    def call_soon(self, fn) -> None:
        """把回调排到消息循环线程执行。

        热键注册必须发在跑消息循环的那个线程上（RegisterHotKey 把热键绑到
        调用者线程的消息队列），所以其它线程不能直接调用 reload()。
        """
        self._pending.append(fn)
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_APP_CALLBACK, 0, 0)

    def _drain(self) -> None:
        while self._pending:
            fn = self._pending.pop(0)
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("消息循环回调出错")

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
