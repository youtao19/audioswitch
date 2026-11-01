"""切换引擎：应用 profile、快照 / 还原、toggle 判定。"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from . import devices as dv
from . import policy
from .config import Config, ConfigError

log = logging.getLogger("aswitch")

DEFAULT_ROLES = (dv.rConsole, dv.rMultimedia)

# 守护进程里热键线程和监听线程都可能切设备，串行化避免抢设备
_APPLY_LOCK = threading.RLock()


class SwitchError(RuntimeError):
    pass


@dataclass
class SwitchResult:
    profile: str
    changed: bool
    render: str
    capture: str | None = None
    warnings: list[str] = field(default_factory=list)


def snapshot() -> dict[tuple[int, int], str]:
    """记录当前所有 (flow, role) 的默认设备 ID。"""
    snap: dict[tuple[int, int], str] = {}
    for flow in (dv.eRender, dv.eCapture):
        for role in dv.ROLES:
            ep = dv.get_default_endpoint(flow, role)
            if ep:
                snap[(flow, role)] = ep.id
    return snap


def desired_targets(cfg: Config, name: str) -> dict[tuple[int, int], str]:
    """profile -> {(flow, role): endpoint_id}。"""
    prof = cfg.profile(name)
    if not prof.render:
        raise ConfigError(f"profile '{name}' 缺少 render")
    render_id = cfg.resolve(prof.render, "播放设备", flow=dv.eRender)
    targets: dict[tuple[int, int], str] = {}
    for role in DEFAULT_ROLES:
        targets[(dv.eRender, role)] = render_id
        if prof.capture:
            targets[(dv.eCapture, role)] = cfg.resolve(prof.capture, "录音设备", flow=dv.eCapture)
    # 通信角色（Discord / 游戏语音）单独映射
    targets[(dv.eRender, dv.rCommunications)] = cfg.resolve(
        prof.comm_render or prof.render, "通信播放设备", flow=dv.eRender
    )
    comm_capture = prof.comm_capture or prof.capture
    if comm_capture:
        targets[(dv.eCapture, dv.rCommunications)] = cfg.resolve(
            comm_capture, "通信录音设备", flow=dv.eCapture
        )
    return targets


def _check_targets(targets: dict[tuple[int, int], str]) -> None:
    known = {ep.id.lower(): ep for ep in dv.enum_endpoints()}
    problems: list[str] = []
    for eid in dict.fromkeys(targets.values()):
        ep = known.get(eid.lower())
        if ep is None:
            problems.append(f"系统中找不到设备 {eid}")
        elif not ep.is_active:
            problems.append(f"{ep.label} 当前状态为 {ep.state_name}（没插好或已禁用）")
    if problems:
        raise SwitchError("；".join(problems))


def apply_profile(cfg: Config, name: str) -> SwitchResult:
    """把系统默认设备切到 profile。目标设备不可用时报错且不做任何改动。"""
    with _APPLY_LOCK:
        return _apply_profile_locked(cfg, name)


def _apply_profile_locked(cfg: Config, name: str) -> SwitchResult:
    targets = desired_targets(cfg, name)
    _check_targets(targets)

    before = snapshot()
    changed = any(
        (before.get(key, "").lower() != eid.lower()) for key, eid in targets.items()
    )
    if changed:
        with policy.PolicyConfig() as pc:
            for (flow, role), eid in targets.items():
                pc.set_default(eid, role)

    warnings: list[str] = []
    after = snapshot()
    for (flow, role), eid in targets.items():
        if after.get((flow, role), "").lower() != eid.lower():
            warnings.append(
                f"{dv.FLOW_NAMES[flow]}/{dv.ROLE_NAMES[role]} 未能切换"
            )

    prof = cfg.profile(name)
    render_id = cfg.resolve(prof.comm_render or prof.render, "播放设备", flow=dv.eRender)
    capture_spec = prof.comm_capture or prof.capture
    capture_id = (
        cfg.resolve(capture_spec, "录音设备", flow=dv.eCapture) if capture_spec else None
    )
    labels = {ep.id.lower(): ep.label for ep in dv.enum_endpoints()}
    return SwitchResult(
        profile=name,
        changed=changed,
        render=labels.get(render_id.lower(), render_id),
        capture=labels.get(capture_id.lower(), capture_id) if capture_id else None,
        warnings=warnings,
    )


def restore(snap: dict[tuple[int, int], str]) -> list[str]:
    """按快照还原默认设备（尽力而为，跳过已不可用的设备）。"""
    with _APPLY_LOCK:
        known = {ep.id.lower(): ep for ep in dv.enum_endpoints()}
        warnings: list[str] = []
        with policy.PolicyConfig() as pc:
            for (flow, role), eid in snap.items():
                ep = known.get(eid.lower())
                if ep is None or not ep.is_active:
                    warnings.append(f"跳过还原 {eid}（设备不可用）")
                    continue
                try:
                    pc.set_default(eid, role)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"还原 {ep.label} 失败: {exc}")
        return warnings


def matches_profile(cfg: Config, name: str) -> bool:
    """当前默认设备是否已经等于该 profile（只看 console/multimedia）。"""
    try:
        targets = desired_targets(cfg, name)
    except (ConfigError, KeyError):
        return False
    cur = snapshot()
    keys = [k for k in targets if k[1] != dv.rCommunications]
    if not keys:
        return False
    return all(cur.get(k, "").lower() == targets[k].lower() for k in keys)


def toggle_target(cfg: Config) -> str:
    a, b = cfg.toggle_pair()
    if matches_profile(cfg, a):
        return b
    return a
