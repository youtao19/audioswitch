"""最小 COM 封装 —— 纯 ctypes，零第三方依赖。

只提供本项目需要的几件事：
  * GUID / PROPERTYKEY / PROPVARIANT 结构
  * CoInitializeEx（按线程）
  * CoCreateInstance
  * 手工 vtable 调用 + Release
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import POINTER, byref, c_int, c_ulong, c_void_p, c_wchar_p
from ctypes import wintypes

ole32 = ctypes.WinDLL("ole32", use_last_error=True)

HRESULT = ctypes.c_long
S_OK = 0
RPC_E_CHANGED_MODE = 0x80010106

CLSCTX_ALL = 0x17
COINIT_APARTMENTTHREADED = 0x2
STGM_READ = 0


class COMError(RuntimeError):
    def __init__(self, hr: int, what: str = "COM call"):
        code = hr & 0xFFFFFFFF
        super().__init__(f"{what} 失败 (HRESULT=0x{code:08X})")
        self.hr = hr


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __str__(self) -> str:  # {XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}
        buf = ctypes.create_unicode_buffer(64)
        ole32.StringFromGUID2(byref(self), buf, 64)
        return buf.value


ole32.CLSIDFromString.argtypes = [c_wchar_p, POINTER(GUID)]
ole32.CLSIDFromString.restype = HRESULT
ole32.StringFromGUID2.argtypes = [POINTER(GUID), c_wchar_p, c_int]
ole32.StringFromGUID2.restype = c_int
ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = HRESULT
ole32.CoCreateInstance.argtypes = [
    POINTER(GUID),
    c_void_p,
    wintypes.DWORD,
    POINTER(GUID),
    POINTER(c_void_p),
]
ole32.CoCreateInstance.restype = HRESULT
ole32.PropVariantClear.argtypes = [c_void_p]
ole32.PropVariantClear.restype = HRESULT
ole32.CoTaskMemFree.argtypes = [c_void_p]
ole32.CoTaskMemFree.restype = None


def guid(text: str) -> GUID:
    g = GUID()
    if ole32.CLSIDFromString(c_wchar_p(text), byref(g)) < 0:
        raise ValueError(f"无效的 GUID: {text}")
    return g


def check(hr: int, what: str = "COM call") -> int:
    if hr < 0:
        raise COMError(hr, what)
    return hr


_tls = threading.local()


def co_initialize() -> None:
    """在当前线程初始化 COM（每个线程只需一次）。"""
    if getattr(_tls, "ready", False):
        return
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr == RPC_E_CHANGED_MODE:
        # 该线程已经是别的 apartment 模型，可用，但不要 Uninitialize
        _tls.ready = True
        return
    check(hr, "CoInitializeEx")
    _tls.ready = True


def create_instance(clsid: str, iid: str) -> c_void_p:
    """CoCreateInstance，返回接口指针（c_void_p）。"""
    co_initialize()
    obj = c_void_p()
    hr = ole32.CoCreateInstance(
        byref(guid(clsid)), None, CLSCTX_ALL, byref(guid(iid)), byref(obj)
    )
    check(hr, f"CoCreateInstance({clsid})")
    if not obj:
        raise COMError(-1, f"CoCreateInstance({clsid}) 返回空指针")
    return obj


def vcall(ptr: c_void_p, index: int, argtypes: list, *args, ret=HRESULT):
    """调用 COM 接口虚表第 index 个方法。"""
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    entry = vtbl[index]
    if not entry:
        raise COMError(-1, f"vtable[{index}] 为空")
    proto = ctypes.WINFUNCTYPE(ret, c_void_p, *argtypes)
    return proto(entry)(ptr, *args)


def release(ptr: c_void_p) -> None:
    if ptr:
        try:
            vcall(ptr, 2, [], ret=c_ulong)  # IUnknown::Release
        except OSError:
            pass
