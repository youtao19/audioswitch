"""命令行入口。"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
from ctypes import byref, wintypes
from pathlib import Path

from . import __version__
from . import config as cfgmod
from . import devices as dv
from . import engine
from . import hotkey as hk
from . import watcher as wt

QUIT_EVENT_NAME = "aswitch-quit-event"
EVENT_MODIFY_STATE = 0x0002

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.SetEvent.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD

log = logging.getLogger("aswitch")


def daemon_running() -> bool:
    """守护进程是否在跑（靠它创建的具名事件判断）。"""
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, QUIT_EVENT_NAME)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True



# ---------------------------------------------------------------- 基础设施
def _fix_streams() -> None:
    # pythonw.exe 下 stdout/stderr 是 None，给个空流，避免 print/log 报错
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
            setattr(sys, name, devnull)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def setup_logging(verbose: bool, logfile: Path | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = []
    if sys.stderr is not None and sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    if logfile is not None:
        try:
            handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers or [logging.NullHandler()],
        force=True,
    )


def _load_config(required: bool = True) -> cfgmod.Config | None:
    try:
        return cfgmod.Config.load()
    except cfgmod.ConfigError as exc:
        print(exc)
        if required:
            sys.exit(2)
        return None


def _alias_hint(cfg: cfgmod.Config | None, endpoint_id: str) -> str:
    if cfg is None:
        return ""
    alias = cfg.alias_of(endpoint_id)
    return f" ({alias})" if alias else ""


# ---------------------------------------------------------------- 命令
def cmd_list(args: argparse.Namespace) -> int:
    cfg = _load_config(required=False)
    eps = dv.enum_endpoints()
    defaults = {}
    for flow in (dv.eRender, dv.eCapture):
        for role in dv.ROLES:
            ep = dv.get_default_endpoint(flow, role)
            if ep:
                defaults.setdefault(ep.id.lower(), []).append(
                    f"{dv.ROLE_NAMES[role]}" if flow == dv.eRender else f"mic/{dv.ROLE_NAMES[role]}"
                )

    if args.json:
        print(json.dumps([
            {
                "id": ep.id, "flow": ep.flow_name, "state": ep.state_name,
                "name": ep.name, "adapter": ep.adapter,
                "alias": cfg.alias_of(ep.id) if cfg else None,
                "roles": defaults.get(ep.id.lower(), []),
            }
            for ep in eps
        ], ensure_ascii=False, indent=2))
        return 0

    for flow, title in ((dv.eRender, "播放设备 (render)"), (dv.eCapture, "录音设备 (capture)")):
        rows = [ep for ep in eps if ep.flow == flow]
        if not args.all:
            rows = [ep for ep in rows if ep.is_active]
        print(f"\n=== {title} ===")
        if not rows:
            print("  (无)")
            continue
        for ep in rows:
            mark = "*" if defaults.get(ep.id.lower()) else " "
            alias = cfg.alias_of(ep.id) if cfg else None
            alias_txt = f"[{alias}] " if alias else ""
            roles = ("默认: " + ",".join(defaults[ep.id.lower()])) if defaults.get(ep.id.lower()) else ""
            print(f" {mark} {alias_txt}{ep.label:<46} {ep.state_name:<10} {roles}")
            print(f"     {ep.id}")
    if not args.all:
        print("\n(只显示在线设备；加 --all 看全部)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load_config(required=False)
    print(f"配置文件: {cfgmod.config_path()}")
    running = daemon_running()
    print("守护进程: " + ("[运行中]" if running else "[未运行] 托盘/热键/自动切换都没生效，执行 aswitch start"))
    if cfg is not None and cfg.path.exists():
        names = ", ".join(cfg.profiles)
        print(f"可用 profile: {names}")
    print()
    for flow, title in ((dv.eRender, "播放"), (dv.eCapture, "录音")):
        for role in dv.ROLES:
            ep = dv.get_default_endpoint(flow, role)
            label = ep.label if ep else "(无)"
            alias = _alias_hint(cfg, ep.id) if ep else ""
            print(f"  {title}/{dv.ROLE_NAMES[role]:<15} {label}{alias}")
    if cfg is not None:
        matched = [name for name in cfg.profiles if engine.matches_profile(cfg, name)]
        if matched:
            print(f"\n当前设备符合 profile: {', '.join(matched)}")
        enabled = [r for r in cfg.rules if r.enabled]
        print(f"自动切换规则: {len(enabled)} 条" if enabled else "自动切换规则: 无")
        if running and enabled:
            watcher = wt.RuleWatcher(cfg, dry_run=True)
            hit = watcher.decide(wt.running_exes())
            print("当前命中的规则: " + (f"{hit[1]} -> {hit[0]}" if hit else "无"))
        elif enabled and not running:
            print("  ↑ 规则已配置，但守护进程没在跑，所以不会自动切")
    return 0


def cmd_use(args: argparse.Namespace) -> int:
    cfg = _load_config()
    assert cfg is not None
    try:
        res = engine.apply_profile(cfg, args.profile)
    except Exception as exc:  # noqa: BLE001
        print(f"切换失败: {exc}")
        return 1
    print(f"已切换到 profile '{res.profile}'")
    print(f"  播放 -> {res.render}")
    if res.capture:
        print(f"  录音 -> {res.capture}")
    for warning in res.warnings:
        print(f"  警告: {warning}")
    return 0


def cmd_toggle(args: argparse.Namespace) -> int:
    cfg = _load_config()
    assert cfg is not None
    try:
        target = engine.toggle_target(cfg)
    except cfgmod.ConfigError as exc:
        print(exc)
        return 2
    return cmd_use(argparse.Namespace(profile=target))


def cmd_init(args: argparse.Namespace) -> int:
    path = cfgmod.config_path()
    if path.exists() and not args.force:
        print(f"配置文件已存在: {path}\n如需覆盖请加 --force")
        return 1
    text, hint = cfgmod.build_init_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"已生成配置: {path}\n")
    print(hint)
    print("\n下一步:")
    print("  python -m aswitch status          # 看当前默认设备")
    print("  python -m aswitch use headset     # 手动切一次，确认声音对不对")
    print("  python -m aswitch daemon          # 启动托盘 + 热键 + 自动切换")
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    """先切设备，再启动程序（对"启动时锁死音频设备"的游戏最有效）。"""
    cfg = _load_config()
    assert cfg is not None
    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("用法: aswitch launch --profile headset -- \"C:\\path\\game.exe\"")
        return 2
    try:
        res = engine.apply_profile(cfg, args.profile)
        print(f"已切换到 '{res.profile}' ({res.render})，启动: {' '.join(cmd)}")
    except Exception as exc:  # noqa: BLE001
        print(f"切换失败，仍继续启动: {exc}")
    return subprocess.call(cmd, shell=False)


def cmd_start(args: argparse.Namespace) -> int:
    """后台启动 daemon（不占终端，关掉终端也活着）。"""
    if daemon_running():
        print("守护进程已经在运行了。")
        return 0
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pythonw) if pythonw.exists() else sys.executable
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        [exe, "-m", "aswitch", "daemon"],
        cwd=str(cfgmod.PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    for _ in range(20):
        time.sleep(0.25)
        if daemon_running():
            print(f"守护进程已启动（日志: {cfgmod.PROJECT_ROOT / 'aswitch.log'}）")
            return 0
    print("启动失败，请看 aswitch.log 或用 --verbose 前台运行排查")
    return 1


def cmd_quit(args: argparse.Namespace) -> int:
    """通知正在运行的 daemon 退出。"""
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, QUIT_EVENT_NAME)
    if not handle:
        print("没有正在运行的 aswitch daemon。")
        return 1
    kernel32.SetEvent(handle)
    kernel32.CloseHandle(handle)
    print("已通知 daemon 退出。")
    return 0


def cmd_help(args: argparse.Namespace) -> int:
    """常用命令速查（中文文本放在 Python 里，避开 .cmd 的编码坑）。"""
    cfg = cfgmod.Config.load() if cfgmod.config_path().exists() else None
    names = ", ".join(cfg.profiles) if cfg else "(还没有配置)"
    print(
        f"""
可用 profile: {names}

  use <名字>      切到某个 profile（例: use 耳机）
  toggle          在两个 profile 之间切换
  status          当前默认设备 + 守护进程状态
  list [-a]       列出音频设备（-a 含未连接）
  start / quit    启动 / 停止后台守护进程
  watch           只打印规则命中情况（查进程名）
  launch          aswitch launch --profile 耳机 -- "C:\\game\\game.exe"
  config --open   打开配置文件
  quit            退出守护进程

配置文件: {cfgmod.config_path()}
"""
    )
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    path = cfgmod.config_path()
    print(path)
    if args.open and path.exists():
        os.startfile(str(path))  # noqa: S606
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    setup_logging(args.verbose, cfgmod.PROJECT_ROOT / "aswitch.log")
    cfg = _load_config()
    assert cfg is not None

    mutex = kernel32.CreateMutexW(None, False, "Global\\aswitch-daemon")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        print("aswitch daemon 已经在运行了。")
        return 1

    from . import tray as ui

    state: dict = {"tray": None, "watcher": None, "manager": None, "ctrl_handler": None}

    def notify(title: str, text: str, warning: bool = False) -> None:
        if not cfg.setting("notify"):
            return
        tray = state["tray"]
        if tray is not None and tray.available:
            tray.balloon(title, text, warning=warning)

    # ---------------- 动作 ----------------
    def switch_to(name: str, source: str = "手动") -> None:
        try:
            res = engine.apply_profile(cfg, name)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 切换到 %s 失败: %s", source, name, exc)
            notify("切换失败", str(exc), warning=True)
            return
        log.info("%s -> profile '%s'(%s)", source, name, res.render)
        notify(f"已切换到 {name}", res.render)

    def do_toggle() -> None:
        try:
            switch_to(engine.toggle_target(cfg), "热键 toggle")
        except cfgmod.ConfigError as exc:
            log.warning("%s", exc)

    def quit_all() -> None:
        log.info("收到退出请求")
        manager = state["manager"]
        if manager is not None:
            manager.stop()

    # ---------------- 热键 ----------------
    def build_bindings() -> dict:
        bindings: dict = {}
        if args.no_hotkeys:
            return bindings

        def bind(name: str, spec: str):
            def action() -> None:
                switch_to(name, f"热键 {spec}")

            action.__name__ = f"use {name}"
            return action

        for spec, action in cfg.hotkeys.items():
            action = action.strip()
            if action == "toggle":
                bindings[spec] = do_toggle
            elif action in cfg.profiles:
                bindings[spec] = bind(action, spec)
            else:
                log.warning("热键 %s 指向未知 profile: %r", spec, action)
        return bindings

    manager = hk.HotkeyManager(build_bindings())
    state["manager"] = manager

    # ---------------- 托盘 ----------------
    def build_menu() -> list:
        items = []
        for index, name in enumerate(cfg.profiles):
            try:
                checked = engine.matches_profile(cfg, name)
            except Exception:  # noqa: BLE001
                checked = False
            items.append(ui.MenuItem(label=name, command=100 + index, checked=checked))
        items.append(ui.MenuItem(label="切换（toggle）", command=200))
        items.append(ui.MenuItem(separator=True))
        items.append(ui.MenuItem(label="重载配置", command=900))
        items.append(ui.MenuItem(label="打开配置文件", command=901))
        items.append(ui.MenuItem(label="退出", command=902))
        return items

    def reload_config() -> None:
        nonlocal cfg
        try:
            cfg = cfgmod.Config.load()
        except cfgmod.ConfigError as exc:
            log.warning("重载配置失败: %s", exc)
            notify("配置错误", str(exc), warning=True)
            return
        watcher = state["watcher"]
        if watcher is not None:
            watcher.stop()
            watcher.join(timeout=3)
        if not args.no_watch:
            watcher = wt.RuleWatcher(cfg, notify=notify, dry_run=args.dry_run)
            watcher.start()
            state["watcher"] = watcher
        manager.reload(build_bindings())
        log.info("配置已重载")

    def on_command(command: int) -> None:
        if 100 <= command < 200:
            names = list(cfg.profiles)
            index = command - 100
            if index < len(names):
                switch_to(names[index], "托盘")
        elif command == 200:
            do_toggle()
        elif command == 900:
            reload_config()
        elif command == 901:
            if cfg.path.exists():
                os.startfile(str(cfg.path))  # noqa: S606
        elif command == 902:
            quit_all()

    if not args.no_tray:
        tray = ui.TrayIcon("audioswitch", build_menu, on_command)
        if tray.create():
            state["tray"] = tray
            log.info("托盘图标已创建")
        else:
            log.warning("托盘不可用，只用热键和自动切换")

    # ---------------- 进程监听 ----------------
    if not args.no_watch:
        watcher = wt.RuleWatcher(cfg, notify=notify, dry_run=args.dry_run)
        watcher.start()
        state["watcher"] = watcher

    # ---------------- 退出信号（aswitch quit / Ctrl+C） ----------------
    ctrl_handler = _install_console_ctrl_handler(manager.stop)
    state["ctrl_handler"] = ctrl_handler  # 保持引用，避免回调被 GC
    quit_event = kernel32.CreateEventW(None, False, False, QUIT_EVENT_NAME)
    if quit_event:
        def _wait_quit() -> None:
            kernel32.WaitForSingleObject(quit_event, 0xFFFFFFFF)
            log.info("收到 quit 信号，准备退出")
            manager.stop()

        threading.Thread(target=_wait_quit, name="aswitch-quit", daemon=True).start()

    log.info("audioswitch 守护进程已就绪（托盘=%s 热键=%d 规则=%d）",
             bool(state["tray"]), len(build_bindings()),
             len([r for r in cfg.rules if r.enabled]))
    try:
        manager.run()  # 阻塞：消息循环，负责热键与托盘菜单
    except KeyboardInterrupt:
        pass
    finally:
        watcher = state["watcher"]
        if watcher is not None:
            watcher.stop()
        if state["tray"] is not None:
            state["tray"].destroy()
        log.info("audioswitch 已退出")
    return 0


_CTRL_HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)


def _install_console_ctrl_handler(callback):
    def handler(event: int) -> bool:
        if event in (0, 1):  # CTRL_C_EVENT / CTRL_BREAK_EVENT
            callback()
            return True
        return False

    ref = _CTRL_HANDLER(handler)
    kernel32.SetConsoleCtrlHandler(ref, True)
    return ref


def cmd_watch(args: argparse.Namespace) -> int:
    """只看不改：打印规则命中情况，用于调试匹配。"""
    cfg = _load_config()
    assert cfg is not None
    setup_logging(args.verbose)
    watcher = wt.RuleWatcher(cfg, dry_run=True)
    last = None
    print("监听进程（Ctrl+C 退出）...")
    try:
        while True:
            exes = wt.running_exes()
            decision = watcher.decide(exes)
            if decision != last:
                if decision:
                    print(f"命中 {decision[1]} -> profile {decision[0]}")
                elif last:
                    print("规则进程已退出")
                last = decision
            watcher._stop_event.wait(float(cfg.setting("poll_interval") or 1.0))
    except KeyboardInterrupt:
        return 0


# ---------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aswitch", description="快速切换 / 自动切换 Windows 默认音频设备"
    )
    parser.add_argument("--version", action="version", version=f"audioswitch {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出音频设备")
    p.add_argument("-a", "--all", action="store_true", help="包含未连接/禁用的设备")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("status", help="当前默认设备")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("use", help="切换到某个 profile")
    p.add_argument("profile")
    p.set_defaults(func=cmd_use)

    p = sub.add_parser("toggle", help="在两个 profile 间切换")
    p.set_defaults(func=cmd_toggle)

    p = sub.add_parser("init", help="生成配置模板")
    p.add_argument("-f", "--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("launch", help="先切设备再启动程序")
    p.add_argument("--profile", required=True)
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("daemon", help="后台守护：托盘 + 热键 + 自动切换")
    p.add_argument("--no-tray", action="store_true")
    p.add_argument("--no-hotkeys", action="store_true")
    p.add_argument("--no-watch", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="只打印，不真的切设备")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("watch", help="调试规则匹配（不切设备）")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("start", help="后台启动 daemon")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("quit", help="让正在运行的 daemon 退出")
    p.set_defaults(func=cmd_quit)

    p = sub.add_parser("help", help="常用命令速查")
    p.set_defaults(func=cmd_help)

    p = sub.add_parser("config", help="打印配置文件路径")
    p.add_argument("--open", action="store_true", help="用默认程序打开")
    p.set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    _fix_streams()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except cfgmod.ConfigError as exc:
        print(f"配置错误: {exc}")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
