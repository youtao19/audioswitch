"""通过未文档化的 IPolicyConfig 接口设置系统默认音频设备。

Windows 没有公开 API 来做这件事；所有工具（SoundSwitch、AudioSwitcher、
nircmd 等）走的都是同一个 COM 接口 IPolicyConfig::SetDefaultEndpoint。
"""
from __future__ import annotations

from ctypes import c_int, c_wchar_p

from .com import check, create_instance, release, vcall

# {870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}
CLSID_CPolicyConfigClient = "{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}"
# {F8679F50-850A-41CF-9C72-430F290290C8}
IID_IPolicyConfig = "{F8679F50-850A-41CF-9C72-430F290290C8}"

# IPolicyConfig 的 vtable（IUnknown 3 个 + 10 个方法后是 SetDefaultEndpoint）
_VTBL_SET_DEFAULT_ENDPOINT = 13
_VTBL_SET_ENDPOINT_VISIBILITY = 14

ERoles = {
    0: "console",
    1: "multimedia",
    2: "communications",
}
ALL_ROLES = (0, 1, 2)


class PolicyConfig:
    """默认设备设置器，用完请 close() 或使用 with。"""

    def __init__(self) -> None:
        self._ptr = create_instance(CLSID_CPolicyConfigClient, IID_IPolicyConfig)

    def set_default(self, endpoint_id: str, role: int) -> None:
        hr = vcall(
            self._ptr,
            _VTBL_SET_DEFAULT_ENDPOINT,
            [c_wchar_p, c_int],
            endpoint_id,
            role,
        )
        check(hr, f"SetDefaultEndpoint({endpoint_id}, role={role})")

    def close(self) -> None:
        if self._ptr:
            release(self._ptr)
            self._ptr = None

    def __enter__(self) -> "PolicyConfig":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def set_default(endpoint_id: str, roles=ALL_ROLES) -> None:
    with PolicyConfig() as pc:
        for role in roles:
            pc.set_default(endpoint_id, role)
