"""把 exe 的图标取出来，转成 PNG（供 tkinter PhotoImage 用）。

零依赖：SHGetFileInfoW 拿 HICON → GetDIBits 取像素 → 手写 PNG 编码。
图标可能给的是 32×32（甚至 48×48），这里按真实尺寸读取后 alpha 加权缩到目标尺寸。
"""
from __future__ import annotations

import ctypes
import logging
import struct
import zlib
from ctypes import byref, wintypes

from . import appnames

log = logging.getLogger("aswitch")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

SHGFI_ICON = 0x00000100
SHGFI_SMALLICON = 0x00000001
SHGFI_USEFILEATTRIBUTES = 0x00000010
BI_RGB = 0
DIB_RGB_COLORS = 0
FILE_ATTRIBUTE_NORMAL = 0x00000080

shell32.SHGetFileInfoW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.UINT, wintypes.UINT
]
shell32.SHGetFileInfoW.restype = ctypes.c_size_t
user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.c_void_p]
user32.GetIconInfo.restype = wintypes.BOOL
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
]


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", wintypes.HICON),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", wintypes.WCHAR * 260),
        ("szTypeName", wintypes.WCHAR * 80),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", wintypes.LONG),
        ("bmWidth", wintypes.LONG),
        ("bmHeight", wintypes.LONG),
        ("bmWidthBytes", wintypes.LONG),
        ("bmPlanes", wintypes.WORD),
        ("bmBitsPixel", wintypes.WORD),
        ("bmBits", ctypes.c_void_p),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _png(width: int, height: int, rgba: bytes) -> bytes:
    """最小 PNG 编码器（RGBA8、滤波器 0）。"""
    rows = b"".join(
        bytes((0,)) + rgba[y * width * 4 : (y + 1) * width * 4] for y in range(height)
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    signature = bytes((137, 80, 78, 71, 13, 10, 26, 10))  # PNG magic
    return (
        signature
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def _bitmap_size(hbm) -> tuple[int, int]:
    bmp = BITMAP()
    if hbm and gdi32.GetObjectW(hbm, ctypes.sizeof(BITMAP), byref(bmp)):
        if bmp.bmWidth > 0 and bmp.bmHeight > 0:
            return int(bmp.bmWidth), int(bmp.bmHeight)
    return 0, 0


def _read_dib(hbm, width: int, height: int, bpp: int) -> bytes | None:
    if not hbm or width <= 0 or height <= 0:
        return None
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height  # 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = bpp
    bmi.bmiHeader.biCompression = BI_RGB
    bmi.bmiHeader.biSizeImage = width * height * 4
    stride = ((width * bpp + 31) // 32) * 4
    buf = (ctypes.c_ubyte * (stride * height))()
    screen = user32.GetDC(None)
    try:
        got = gdi32.GetDIBits(screen, hbm, 0, height, buf, byref(bmi), DIB_RGB_COLORS)
    finally:
        user32.ReleaseDC(None, screen)
    return bytes(buf) if got else None


def _scale(rgba: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes:
    """alpha 加权的盒式缩放（避免透明边缘发黑）。"""
    out = bytearray(dw * dh * 4)
    for dy in range(dh):
        y0 = dy * sh // dh
        y1 = max(y0 + 1, (dy + 1) * sh // dh)
        for dx in range(dw):
            x0 = dx * sw // dw
            x1 = max(x0 + 1, (dx + 1) * sw // dw)
            sa = sr = sg = sb = 0
            count = 0
            for sy in range(y0, y1):
                base = sy * sw * 4
                for sx in range(x0, x1):
                    i = base + sx * 4
                    a = rgba[i + 3]
                    sa += a
                    sr += rgba[i] * a
                    sg += rgba[i + 1] * a
                    sb += rgba[i + 2] * a
                    count += 1
            o = (dy * dw + dx) * 4
            if count and sa:
                out[o] = min(255, sr // sa)
                out[o + 1] = min(255, sg // sa)
                out[o + 2] = min(255, sb // sa)
                out[o + 3] = sa // count
            else:
                out[o : o + 4] = bytes((0, 0, 0, 0))
    return bytes(out)


def _hicon_rgba(hicon, size: int) -> bytes | None:
    info = ICONINFO()
    if not user32.GetIconInfo(hicon, byref(info)):
        return None
    try:
        width, height = _bitmap_size(info.hbmColor or info.hbmMask)
        if width <= 0:
            width = height = size
        color = _read_dib(info.hbmColor, width, height, 32)
        mask = _read_dib(info.hbmMask, width, height, 1)
    finally:
        if info.hbmColor:
            gdi32.DeleteObject(info.hbmColor)
        if info.hbmMask:
            gdi32.DeleteObject(info.hbmMask)

    mask_stride = ((width + 31) // 32) * 4
    rgba = bytearray(width * height * 4)

    def transparent_at(x: int, y: int) -> bool:
        if not mask:
            return False
        return bool((mask[y * mask_stride + x // 8] >> (7 - (x % 8))) & 1)

    if color is None:
        if not mask:
            return None
        for y in range(height):
            for x in range(width):
                i = (y * width + x) * 4
                rgba[i : i + 4] = (
                    bytes((0, 0, 0, 0)) if transparent_at(x, y) else bytes((0, 0, 0, 255))
                )
    else:
        has_alpha = any(color[i] for i in range(3, len(color), 4))
        for y in range(height):
            row = y * width * 4
            for x in range(width):
                i = row + x * 4
                rgba[i] = color[i + 2]      # B -> R
                rgba[i + 1] = color[i + 1]  # G
                rgba[i + 2] = color[i]      # R -> B
                if has_alpha:
                    rgba[i + 3] = color[i + 3]
                elif transparent_at(x, y):
                    rgba[i + 3] = 0
                else:
                    rgba[i + 3] = 255

    if width != size or height != size:
        return _scale(bytes(rgba), width, height, size, size)
    return bytes(rgba)


def icon_png(exe: str, pid: int | None = None, size: int = 16) -> bytes | None:
    """返回 size×size RGBA 的 PNG 数据；取不到返回 None。"""
    path = appnames.exe_path(exe, pid=pid)
    shai = SHFILEINFOW()
    flags = SHGFI_ICON | SHGFI_SMALLICON
    if path:
        shell32.SHGetFileInfoW(path, 0, byref(shai), ctypes.sizeof(shai), flags)
    if not shai.hIcon:
        # 找不到路径就用系统通用的 exe 图标
        shell32.SHGetFileInfoW(
            "a.exe", FILE_ATTRIBUTE_NORMAL, byref(shai), ctypes.sizeof(shai),
            flags | SHGFI_USEFILEATTRIBUTES,
        )
    if not shai.hIcon:
        return None
    try:
        rgba = _hicon_rgba(shai.hIcon, size)
        return _png(size, size, rgba) if rgba else None
    except Exception:  # noqa: BLE001
        log.debug("提取图标失败: %s", exe, exc_info=True)
        return None
    finally:
        user32.DestroyIcon(shai.hIcon)
