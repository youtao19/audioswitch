"""枚举音频端点（播放/录音）并读取名字、状态、默认设备。"""
from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_int, c_uint, c_void_p, c_wchar_p, wintypes
from dataclasses import dataclass
from typing import Iterable

from .com import (GUID, HRESULT, check, co_initialize, create_instance, guid,
                  ole32, release, vcall)

CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IMMDeviceCollection = "{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}"
IID_IMMDevice = "{D666063F-1587-4E43-81F1-B948E807363F}"
IID_IPropertyStore = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"

# EDataFlow
eRender = 0
eCapture = 1
eAll = 2
FLOW_NAMES = {eRender: "render", eCapture: "capture"}

# ERole
rConsole = 0
rMultimedia = 1
rCommunications = 2
ROLES = (rConsole, rMultimedia, rCommunications)
ROLE_NAMES = {rConsole: "console", rMultimedia: "multimedia", rCommunications: "communications"}

# DEVICE_STATE
DEVICE_STATE_ACTIVE = 0x1
DEVICE_STATE_DISABLED = 0x2
DEVICE_STATE_NOTPRESENT = 0x4
DEVICE_STATE_UNPLUGGED = 0x8
DEVICE_STATE_ALL = 0xF

STATE_NAMES = {
    DEVICE_STATE_ACTIVE: "active",
    DEVICE_STATE_DISABLED: "disabled",
    DEVICE_STATE_NOTPRESENT: "notpresent",
    DEVICE_STATE_UNPLUGGED: "unplugged",
}

STGM_READ = 0
VT_LPWSTR = 31

PKEY_FMTID_DEVICE = "{a45c254e-df1c-4efd-8020-67d146a850e0}"
PKEY_Device_DeviceDesc = (PKEY_FMTID_DEVICE, 2)
PKEY_Device_FriendlyName = (PKEY_FMTID_DEVICE, 14)
PKEY_DeviceInterface_FriendlyName = ("{b3f8fa53-0004-438e-9003-51a46e139bfc}", 6)


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("v1", c_void_p),
        ("v2", c_void_p),
    ]


@dataclass
class Endpoint:
    id: str
    flow: int
    state: int
    name: str        # 友好名，如 "扬声器 (Misiom-Scorpio)"
    device: str      # 设备描述，如 "扬声器"
    adapter: str     # 接口/适配器名，如 "Misiom-Scorpio"

    @property
    def is_active(self) -> bool:
        return self.state == DEVICE_STATE_ACTIVE

    @property
    def state_name(self) -> str:
        return STATE_NAMES.get(self.state, f"0x{self.state:X}")

    @property
    def flow_name(self) -> str:
        return FLOW_NAMES.get(self.flow, str(self.flow))

    @property
    def label(self) -> str:
        """人类可读标签：优先用适配器名区分同名设备。"""
        if self.adapter and self.adapter not in self.name:
            return f"{self.name} [{self.adapter}]"
        return self.name


def _enumerator() -> c_void_p:
    co_initialize()
    return create_instance(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator)


def _pkey(spec: tuple[str, int]) -> PROPERTYKEY:
    return PROPERTYKEY(guid(spec[0]), spec[1])


def _get_prop_string(store: c_void_p, spec: tuple[str, int]) -> str:
    key = _pkey(spec)
    pv = PROPVARIANT()
    hr = vcall(store, 5, [POINTER(PROPERTYKEY), POINTER(PROPVARIANT)],
               byref(key), byref(pv))
    if hr < 0:
        return ""
    try:
        if pv.vt == VT_LPWSTR and pv.v1:
            return ctypes.cast(pv.v1, c_wchar_p).value or ""
        return ""
    finally:
        ole32.PropVariantClear(byref(pv))


def _read_device(dev: c_void_p, flow: int) -> Endpoint:
    # IMMDevice::GetId (vtable 5)
    pid = c_wchar_p()
    check(vcall(dev, 5, [POINTER(c_wchar_p)], byref(pid)), "IMMDevice::GetId")
    endpoint_id = pid.value or ""
    raw = ctypes.cast(pid, c_void_p).value
    if raw:
        ole32.CoTaskMemFree(raw)

    # IMMDevice::GetState (vtable 6)
    state = wintypes.DWORD()
    check(vcall(dev, 6, [POINTER(wintypes.DWORD)], byref(state)), "IMMDevice::GetState")

    # IMMDevice::OpenPropertyStore (vtable 4)
    store = c_void_p()
    name = device = adapter = ""
    hr = vcall(dev, 4, [wintypes.DWORD, POINTER(c_void_p)],
               wintypes.DWORD(STGM_READ), byref(store))
    if hr >= 0 and store:
        try:
            name = _get_prop_string(store, PKEY_Device_FriendlyName)
            device = _get_prop_string(store, PKEY_Device_DeviceDesc)
            adapter = _get_prop_string(store, PKEY_DeviceInterface_FriendlyName)
        finally:
            release(store)

    return Endpoint(
        id=endpoint_id,
        flow=flow,
        state=state.value,
        name=name or device or endpoint_id,
        device=device or name,
        adapter=adapter,
    )


def enum_endpoints(flows: Iterable[int] = (eRender, eCapture),
                   states: int = DEVICE_STATE_ALL) -> list[Endpoint]:
    """枚举端点。默认包含未插入/禁用的设备，便于检测「耳机是否接着」。"""
    result: list[Endpoint] = []
    enumerator = _enumerator()
    try:
        for flow in flows:
            coll = c_void_p()
            hr = vcall(enumerator, 3, [c_int, wintypes.DWORD, POINTER(c_void_p)],
                       c_int(flow), wintypes.DWORD(states), byref(coll))
            if hr < 0:
                continue
            try:
                count = c_uint()
                if vcall(coll, 3, [POINTER(c_uint)], byref(count)) < 0:
                    continue
                for i in range(count.value):
                    dev = c_void_p()
                    if vcall(coll, 4, [c_uint, POINTER(c_void_p)],
                             c_uint(i), byref(dev)) < 0:
                        continue
                    try:
                        result.append(_read_device(dev, flow))
                    finally:
                        release(dev)
            finally:
                release(coll)
    finally:
        release(enumerator)
    return result


def get_default_endpoint(flow: int, role: int) -> Endpoint | None:
    """取某个角色（console/multimedia/communications）的当前默认设备。"""
    enumerator = _enumerator()
    try:
        dev = c_void_p()
        hr = vcall(enumerator, 4, [c_int, c_int, POINTER(c_void_p)],
                   c_int(flow), c_int(role), byref(dev))
        if hr < 0 or not dev:
            return None
        try:
            return _read_device(dev, flow)
        finally:
            release(dev)
    finally:
        release(enumerator)


def get_endpoint_by_id(endpoint_id: str) -> Endpoint | None:
    for ep in enum_endpoints():
        if ep.id.lower() == endpoint_id.lower():
            return ep
    return None
