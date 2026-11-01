"""托盘图标 + 右键菜单（Shell_NotifyIcon，纯 ctypes）。"""
from __future__ import annotations

import ctypes
import logging
from ctypes import byref, wintypes
from dataclasses import dataclass

from .com import GUID
from .config import PROJECT_ROOT

log = logging.getLogger("aswitch")

ASSETS_DIR = PROJECT_ROOT / "assets"
ICON_FILE = "aswitch.ico"

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_DESTROY = 0x0002
WM_NULL = 0x0000
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x01, 0x02, 0x03

MF_STRING, MF_SEPARATOR, MF_CHECKED, MF_GRAYED = 0x0000, 0x0800, 0x0008, 0x0001
MF_POPUP = 0x0010
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x0002, 0x0080, 0x0100

IDI_APPLICATION = 32512
ERROR_CLASS_ALREADY_EXISTS = 1410

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXSMICON = 49
SM_CYSMICON = 50

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.RegisterClassW.restype = wintypes.ATOM
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadIconW.restype = wintypes.HICON
user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.LoadImageW.restype = wintypes.HANDLE
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeout", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


@dataclass
class MenuItem:
    label: str = ""
    command: int = 0
    checked: bool = False
    enabled: bool = True
    separator: bool = False
    children: list["MenuItem"] | None = None  # 有 children 就是子菜单


class TrayIcon:
    """用法：create() -> 主线程跑消息循环 -> destroy()。"""

    _class_name = "AsWitchTrayWindow"

    def __init__(self, tooltip: str, menu_provider, on_command) -> None:
        self._tooltip = tooltip[:127]
        self._menu_provider = menu_provider
        self._on_command = on_command
        self.hwnd = None
        self._hicon = None
        self._owns_icon = False
        self._wndproc_ref = None
        self.available = False

    # ---------- 图标 ----------
    def _load_icon(self):
        """优先用 assets/aswitch.ico（按当前 DPI 取合适的尺寸），否则用系统默认图标。"""
        ico = ASSETS_DIR / ICON_FILE
        if ico.exists():
            cx = user32.GetSystemMetrics(SM_CXSMICON)
            cy = user32.GetSystemMetrics(SM_CYSMICON)
            handle = user32.LoadImageW(
                None, str(ico), IMAGE_ICON, cx, cy, LR_LOADFROMFILE
            )
            if handle:
                self._owns_icon = True
                log.info("托盘图标: %s（%dx%d）", ico.name, cx, cy)
                return handle
            log.warning(
                "加载 %s 失败（错误码 %d），改用系统默认图标",
                ico, ctypes.get_last_error(),
            )
        else:
            log.info("未找到 %s，用系统默认图标", ico)
        return user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, wintypes.LPCWSTR))

    # ---------- 生命周期 ----------
    def create(self) -> bool:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        self._wndproc_ref = WNDPROC(self._wndproc)
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = self._class_name
        if not user32.RegisterClassW(byref(wc)):
            err = ctypes.get_last_error()
            if err != ERROR_CLASS_ALREADY_EXISTS:
                log.warning("RegisterClassW 失败，错误码 %d", err)
                return False
        self.hwnd = user32.CreateWindowExW(
            0, self._class_name, self._class_name, 0, 0, 0, 0, 0,
            None, None, hinst, None,
        )
        if not self.hwnd:
            log.warning("CreateWindowExW 失败，错误码 %d", ctypes.get_last_error())
            return False
        self._hicon = self._load_icon()
        nid = self._nid()
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self._hicon
        nid.szTip = self._tooltip
        if not shell32.Shell_NotifyIconW(NIM_ADD, byref(nid)):
            log.warning("Shell_NotifyIcon(NIM_ADD) 失败，托盘不可用")
            return False
        self.available = True
        return True

    def destroy(self) -> None:
        if self.hwnd:
            try:
                shell32.Shell_NotifyIconW(NIM_DELETE, byref(self._nid()))
            except Exception:  # noqa: BLE001
                pass
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self._owns_icon and self._hicon:
            user32.DestroyIcon(self._hicon)
            self._owns_icon = False
        self.available = False

    # ---------- 提示气泡 ----------
    def balloon(self, title: str, text: str, *, warning: bool = False,
                error: bool = False) -> None:
        if not self.available:
            return
        nid = self._nid()
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = NIIF_ERROR if error else (NIIF_WARNING if warning else NIIF_INFO)
        shell32.Shell_NotifyIconW(NIM_MODIFY, byref(nid))

    def set_tooltip(self, text: str) -> None:
        self._tooltip = text[:127]
        if not self.available:
            return
        nid = self._nid()
        nid.uFlags = NIF_TIP
        nid.szTip = self._tooltip
        shell32.Shell_NotifyIconW(NIM_MODIFY, byref(nid))

    # ---------- 内部 ----------
    def _nid(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        return nid

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            event = int(lparam) & 0xFFFF
            if event in (WM_RBUTTONUP, WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._show_menu()
            return 0
        if msg == WM_COMMAND:
            command = int(wparam) & 0xFFFF
            try:
                self._on_command(command)
            except Exception:  # noqa: BLE001
                log.exception("托盘菜单命令 %d 出错", command)
            return 0
        if msg == WM_DESTROY:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _build(self, items, hmenu: int) -> None:
        for item in items:
            if item.separator:
                user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            elif item.children is not None:
                submenu = user32.CreatePopupMenu()
                if not submenu:
                    continue
                self._build(item.children, submenu)
                user32.AppendMenuW(
                    hmenu, MF_POPUP | MF_STRING, int(submenu), item.label
                )
            else:
                flags = MF_STRING
                if item.checked:
                    flags |= MF_CHECKED
                if not item.enabled:
                    flags |= MF_GRAYED
                user32.AppendMenuW(hmenu, flags, item.command, item.label)

    def _show_menu(self) -> None:
        try:
            items = self._menu_provider()
        except Exception:  # noqa: BLE001
            log.exception("生成托盘菜单失败")
            return
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            self._build(items, hmenu)
            point = wintypes.POINT()
            user32.GetCursorPos(byref(point))
            user32.SetForegroundWindow(self.hwnd)
            command = user32.TrackPopupMenu(
                hmenu,
                TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                point.x, point.y, 0, self.hwnd, None,
            )
            user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(hmenu)
        if command:
            try:
                self._on_command(int(command))
            except Exception:  # noqa: BLE001
                log.exception("托盘菜单命令 %d 出错", command)
