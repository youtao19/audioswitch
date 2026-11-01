"""配置文件读写（TOML）。

配置默认放在项目根目录 config.toml，可用环境变量 ASWITCH_CONFIG 覆盖。
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_ENV = "ASWITCH_CONFIG"

DEFAULT_SETTINGS: dict = {
    "poll_interval": 1.0,     # 进程轮询间隔（秒）
    "restore_delay": 3.0,     # 游戏退出后多久还原（秒），避免切图误触发
    "notify": True,           # 托盘气泡提示
    "toggle": [],             # 热键 toggle 用的两个 profile
}

DEFAULT_HOTKEYS = {
    "ctrl+alt+1": "headset",
    "ctrl+alt+2": "speaker",
    "ctrl+alt+3": "toggle",
}


class ConfigError(RuntimeError):
    pass


@dataclass
class Profile:
    name: str
    render: str | None = None
    capture: str | None = None
    comm_render: str | None = None
    comm_capture: str | None = None


@dataclass
class Rule:
    exe: list[str]
    profile: str
    enabled: bool = True

    def matches(self, exes: set[str]) -> str | None:
        """exes 为小写进程名集合，返回命中的进程名。"""
        for pattern in self.exe:
            pat = pattern.lower()
            if "*" in pat or "?" in pat:
                import fnmatch
                for exe in exes:
                    if fnmatch.fnmatch(exe, pat):
                        return exe
            elif pat in exes:
                return pat
        return None


@dataclass
class Config:
    path: Path
    settings: dict = field(default_factory=dict)
    render_devices: dict[str, str] = field(default_factory=dict)
    capture_devices: dict[str, str] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    rules: list[Rule] = field(default_factory=list)
    hotkeys: dict[str, str] = field(default_factory=dict)

    # ---------- 查询 ----------
    def setting(self, key: str):
        return self.settings.get(key, DEFAULT_SETTINGS.get(key))

    def profile(self, name: str) -> Profile:
        if name not in self.profiles:
            raise ConfigError(
                f"profile '{name}' 不存在，可用: {', '.join(self.profiles) or '(无)'}"
            )
        return self.profiles[name]

    def resolve(self, value: str, what: str = "设备", flow: int | None = None) -> str:
        """别名 -> 端点 ID；也接受直接写端点 ID。

        flow 用来避免同名歧义：同一个物理设备在 [render] 和 [capture] 里
        通常用同一个别名，但指向不同的端点 ID。
        """
        if not value:
            raise ConfigError(f"{what} 别名不能为空")
        if value.startswith("{"):
            return value
        from . import devices as dv

        tables = (self.render_devices, self.capture_devices)
        if flow == dv.eCapture:
            tables = (self.capture_devices, self.render_devices)
        for table in tables:
            if value in table:
                return table[value]
        raise ConfigError(f"未知的{what}别名 '{value}'（检查 config.toml）")

    def alias_of(self, endpoint_id: str) -> str | None:
        lid = endpoint_id.lower()
        for alias, eid in list(self.render_devices.items()) + list(self.capture_devices.items()):
            if eid.lower() == lid:
                return alias
        return None

    def toggle_pair(self) -> tuple[str, str]:
        pair = list(self.setting("toggle") or [])
        if len(pair) >= 2:
            return pair[0], pair[1]
        if len(self.profiles) >= 2:
            names = list(self.profiles)
            return names[0], names[1]
        raise ConfigError("需要在 [settings] toggle 里配置两个 profile 才能 toggle")

    # ---------- 读 ----------
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = Path(path or config_path())
        if not path.exists():
            raise ConfigError(
                f"找不到配置文件 {path}\n先运行: python -m aswitch init"
            )
        try:
            with path.open("rb") as f:
                raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} 格式错误: {exc}") from exc

        cfg = cls(path=path)
        settings = dict(DEFAULT_SETTINGS)
        settings.update(raw.get("settings", {}) or {})
        cfg.settings = settings
        cfg.render_devices = dict(raw.get("render", {}) or {})
        cfg.capture_devices = dict(raw.get("capture", {}) or {})

        for name, body in (raw.get("profiles", {}) or {}).items():
            body = body or {}
            cfg.profiles[name] = Profile(
                name=name,
                render=body.get("render"),
                capture=body.get("capture"),
                comm_render=body.get("comm_render"),
                comm_capture=body.get("comm_capture"),
            )
        for body in raw.get("rules", []) or []:
            exes = body.get("exe") or []
            if isinstance(exes, str):
                exes = [exes]
            if not exes or not body.get("profile"):
                continue
            cfg.rules.append(
                Rule(
                    exe=[str(x) for x in exes],
                    profile=body["profile"],
                    enabled=bool(body.get("enabled", True)),
                )
            )
        cfg.hotkeys = {str(k): str(v) for k, v in (raw.get("hotkeys", {}) or {}).items()}
        return cfg

    # ---------- 写 ----------
    def save(self) -> None:
        self.path.write_text(self.to_toml(), encoding="utf-8")

    def to_toml(self) -> str:
        lines: list[str] = []

        def emit_table(name: str, table: dict) -> None:
            if not table:
                return
            lines.append(f"[{name}]")
            for k, v in table.items():
                lines.append(f"{_key(k)} = {_value(v)}")
            lines.append("")

        lines.append("[settings]")
        for k, v in self.settings.items():
            lines.append(f"{_key(k)} = {_value(v)}")
        lines.append("")
        emit_table("render", self.render_devices)
        emit_table("capture", self.capture_devices)
        for name, prof in self.profiles.items():
            lines.append(f"[profiles.{_key(name)}]")
            for key in ("render", "capture", "comm_render", "comm_capture"):
                val = getattr(prof, key)
                if val:
                    lines.append(f"{key} = {_value(val)}")
            lines.append("")
        for rule in self.rules:
            lines.append("[[rules]]")
            lines.append(f"exe = {_value(rule.exe)}")
            lines.append(f"profile = {_value(rule.profile)}")
            if not rule.enabled:
                lines.append("enabled = false")
            lines.append("")
        if self.hotkeys:
            lines.append("[hotkeys]")
            for k, v in self.hotkeys.items():
                lines.append(f"{_value(k)} = {_value(v)}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _key(text: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_\-]+", text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _value(val) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return repr(val)
    if isinstance(val, str):
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(val, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in val) + "]"
    raise TypeError(f"不支持的类型: {type(val)}")


def config_path() -> Path:
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env).expanduser()
    return PROJECT_ROOT / "config.toml"


def slug(text: str) -> str:
    text = re.sub(r"[^0-9a-zA-Z]+", "_", text or "").strip("_").lower()
    return text or "dev"


# ---------- 首次生成 ----------
def build_init_config() -> tuple[str, str]:
    """根据当前系统设备生成初始配置，返回 (toml 文本, 提示信息)。"""
    from . import devices as dv

    active = [ep for ep in dv.enum_endpoints() if ep.is_active]
    renders = [ep for ep in active if ep.flow == dv.eRender]
    captures = [ep for ep in active if ep.flow == dv.eCapture]
    if not renders:
        raise ConfigError("没有找到任何可用的播放设备")

    # 别名：用适配器名（能区分「扬声器 (X)」和「扬声器 (Y)」）；
    # 同一适配器的播放/录音端点共用同一个别名，各写进自己的表里。
    alias_by_adapter: dict[str, str] = {}
    used_names: set[str] = set()
    alias_of: dict[str, str] = {}

    def make_alias(ep: dv.Endpoint) -> str:
        key = ep.adapter or ep.name
        if key in alias_by_adapter:
            return alias_by_adapter[key]
        base = slug(key)
        candidate = base
        n = 2
        while candidate in used_names:
            candidate = f"{base}_{n}"
            n += 1
        used_names.add(candidate)
        alias_by_adapter[key] = candidate
        return candidate

    for ep in renders + captures:
        alias_of[ep.id] = make_alias(ep)

    # 猜测哪个是耳机：既是播放又有录音能力的适配器，且不是当前的音响默认设备
    cur_render = dv.get_default_endpoint(dv.eRender, dv.rConsole)
    cur_comm = dv.get_default_endpoint(dv.eRender, dv.rCommunications)
    capture_adapters = {ep.adapter for ep in captures}

    headset = None
    for ep in renders:
        if cur_render and ep.id == cur_render.id:
            continue
        if ep.adapter in capture_adapters:
            headset = ep
            break
    if headset is None and cur_comm and cur_comm.id != (cur_render.id if cur_render else None):
        headset = next((ep for ep in renders if ep.id == cur_comm.id), None)
    speaker = cur_render or renders[0]

    render_lines = []
    for ep in renders:
        a = alias_of[ep.id]
        if ep.id == speaker.id:
            mark = "   # 当前默认输出（音响）"
        elif headset and ep.id == headset.id:
            mark = "   # 猜测是耳机，请确认"
        else:
            mark = f"   # {ep.name}"
        render_lines.append(f'{a} = "{ep.id}"{mark}')

    capture_lines = []
    for ep in captures:
        capture_lines.append(f'{alias_of[ep.id]} = "{ep.id}"   # {ep.name}')

    headset_alias = alias_of.get(headset.id, "") if headset else ""
    headset_mic = ""
    if headset:
        for ep in captures:
            if ep.adapter == headset.adapter:
                headset_mic = alias_of[ep.id]
                break
    speaker_alias = alias_of[speaker.id]

    profiles = []
    if headset_alias:
        body = f'[profiles.headset]\nrender = "{headset_alias}"\n'
        if headset_mic:
            body += f'capture = "{headset_mic}"\n'
        profiles.append(body)
    profiles.append(f'[profiles.speaker]\nrender = "{speaker_alias}"')

    toggle = ["headset", "speaker"] if headset_alias else ["speaker"]

    text = f"""# ===== audioswitch 配置 =====
# 由 `python -m aswitch init` 生成，可随意修改。
# 设备别名指向系统的「端点 ID」（形如 {{0.0.0.00000000}}.{{GUID}}），
# 不要改成设备名 —— 名字会因驱动/语言变化，ID 不变。

[settings]
poll_interval = 1.0        # 进程轮询间隔（秒）
restore_delay = 3.0        # 规则进程全部退出后，延迟多久还原设备
notify = true              # 切换时弹托盘气泡
toggle = {_value(toggle)}   # 热键 toggle 在这两个 profile 间切换

[render]
{chr(10).join(render_lines)}

[capture]
{chr(10).join(capture_lines) if capture_lines else '# (没有可用的录音设备)'}

{chr(10).join(profiles)}

# 规则：命中的进程在运行时自动切到 profile，全部退出后还原
# 用 `python -m aswitch watch` 可看到实际的进程名（exe 名，不分路径）
[[rules]]
exe = [
  "cs2.exe",
  "csgo.exe",
  "valorant-win64-shipping.exe",
  "league of legends.exe",
  "dota2.exe",
  "tslgame.exe",
  "r5apex.exe",
  "overwatch.exe",
  "genshinimpact.exe",
  "yuanshen.exe",
  "starrail.exe",
  "eldenring.exe",
]
profile = "{"headset" if headset_alias else "speaker"}"
enabled = true

# 全局热键（RegisterHotKey，不注入，反作弊友好）
# 值可以是 profile 名，或 "toggle"
[hotkeys]
"ctrl+alt+1" = "{"headset" if headset_alias else "speaker"}"
"ctrl+alt+2" = "speaker"
"ctrl+alt+3" = "toggle"
"""

    hint = []
    if headset_alias:
        hint.append(f"猜测耳机 = {headset_alias} ({headset.label})")
    hint.append(f"当前默认音响 = {speaker_alias} ({speaker.label})")
    hint.append("请用 `python -m aswitch list` 核对，然后 `python -m aswitch use headset` 试听确认。")
    return text, "\n".join(hint)
