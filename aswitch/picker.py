"""图形化规则管理：搜索、单击增删、一键收录当前程序。

只用标准库（tkinter + ctypes），不引入任何依赖。
"""
from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import subprocess
from ctypes import byref, wintypes
from pathlib import Path

from . import appicon
from . import appnames
from . import config as cfgmod
from . import devices as dv
from . import ipc
from . import watcher as wt

log = logging.getLogger("aswitch")

ROOT = cfgmod.PROJECT_ROOT
UI_STATE_FILE = ROOT / "ui_state.json"

user32 = ctypes.WinDLL("user32", use_last_error=True)

GWL_EXSTYLE = -20
GW_OWNER = 4
WS_EX_TOOLWINDOW = 0x00000080
CREATE_NO_WINDOW = 0x08000000

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]

# 系统外壳 / 输入法 / 本工具自身：加进规则没有意义
SKIP_EXES = {
    "explorer.exe", "applicationframehost.exe", "textinputhost.exe",
    "startmenuexperiencehost.exe", "searchhost.exe", "shellexperiencehost.exe",
    "widgets.exe", "widgetservice.exe", "runtimebroker.exe", "conhost.exe",
    "python.exe", "pythonw.exe",
}

# “一键收录”时的拦截名单：这些明显不是游戏，避免手滑加进规则
NOT_GAME_EXES = {
    "explorer.exe", "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
    "windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    "code.exe", "devenv.exe", "pycharm64.exe", "notepad.exe", "mspaint.exe",
    "wechat.exe", "weixin.exe", "qq.exe", "dingtalk.exe", "python.exe", "pythonw.exe",
}

# ---------- 配色 ----------
BG = "#f1f3f9"
CARD = "#ffffff"
INK = "#1f2430"
MUTED = "#6b7280"
LINE = "#e6e8f0"
BRAND = "#7c5cf7"
BRAND_HOVER = "#6a49f0"
BRAND_LIGHT = "#efeafe"
GREEN = "#0f9d58"
RED = "#d93025"
AMBER = "#b26a00"


def visible_apps() -> list[tuple[str, str, int]]:
    """[(窗口标题, 进程名, pid)]，只取有可见主窗口的程序，按标题排序。"""
    pmap = wt.process_map()
    own_pid = os.getpid()
    found: dict[str, tuple[str, int]] = {}

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = (buf.value or "").strip()
        if not title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, byref(pid))
        if int(pid.value) == own_pid:
            return True
        exe = pmap.get(int(pid.value), "")
        if not exe or exe in SKIP_EXES:
            return True
        found.setdefault(exe, (title, int(pid.value)))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return sorted(
        ((title, exe, pid) for exe, (title, pid) in found.items()),
        key=lambda x: x[0].lower(),
    )


def foreground_app() -> tuple[str, str, int] | None:
    """当前前台窗口 -> (标题, 进程名, pid)；拿不到返回 None。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    title = ""
    if length > 0:
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = (buf.value or "").strip()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, byref(pid))
    exe = wt.process_map().get(int(pid.value), "")
    if not exe:
        return None
    return (title or exe), exe, int(pid.value)


def resolve_shortcut(path: Path) -> Path | None:
    """把 .lnk 解析成目标程序（借 WScript.Shell，免额外依赖）。"""
    if path.suffix.lower() != ".lnk":
        return path
    target = str(path).replace("'", "''")
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('" + target + "'); "
        "$s.TargetPath"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        resolved = (out.stdout or "").strip()
        return Path(resolved) if resolved else None
    except Exception:  # noqa: BLE001
        log.exception("解析快捷方式失败: %s", path)
        return None


def _message_box(text: str, title: str = "audioswitch") -> None:
    user32.MessageBoxW(None, text, title, 0x00000040)  # MB_ICONINFORMATION


def _load_ui_state() -> dict:
    try:
        data = json.loads(UI_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_ui_state(data: dict) -> None:
    try:
        UI_STATE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        log.debug("保存窗口状态失败", exc_info=True)


def _device_label(cfg: cfgmod.Config, profile_name: str, endpoints: dict[str, str]) -> str:
    try:
        prof = cfg.profile(profile_name)
        if not prof.render:
            return "（该 profile 没配置设备）"
        endpoint_id = cfg.resolve(prof.render, flow=dv.eRender)
    except Exception:  # noqa: BLE001
        return "（配置里找不到这个设备）"
    return endpoints.get(endpoint_id.lower(), endpoint_id)


def run(profile: str | None = None) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, ttk
    except Exception as exc:  # noqa: BLE001
        _message_box(f"无法启动图形界面（缺少 tkinter）：\n{exc}")
        return 1

    cfg = cfgmod.Config.load()
    profiles = list(cfg.profiles)
    if not profiles:
        _message_box("config.toml 里还没有任何 profile，先加一个 [profiles.xxx]。")
        return 1

    ui_state = _load_ui_state()
    saved_profile = ui_state.get("profile")
    if profile in profiles:
        initial = profile
    elif saved_profile in profiles:
        initial = saved_profile
    else:
        initial = profiles[0]

    # ---------------------------------------------------------------- 窗口
    window = tk.Tk()
    window.title("audioswitch — 自动切换规则")
    try:
        ico = ROOT / "assets" / "aswitch.ico"
        if ico.exists():
            window.iconbitmap(default=str(ico))
    except Exception:  # noqa: BLE001
        pass
    window.configure(bg=BG)
    window.option_add("*TCombobox*Listbox.background", CARD)
    window.option_add("*TCombobox*Listbox.foreground", INK)
    window.option_add("*TCombobox*Listbox.selectBackground", BRAND_LIGHT)
    window.option_add("*TCombobox*Listbox.selectForeground", INK)
    window.option_add("*TCombobox*Listbox.font", ("Microsoft YaHei UI", 10))
    window.minsize(700, 460)

    style = ttk.Style(window)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    font = ("Microsoft YaHei UI", 10)
    style.configure(".", background=BG, foreground=INK, font=font)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=INK, font=font)
    style.configure("Card.TLabel", background=CARD, foreground=INK, font=font)
    style.configure("Title.TLabel", background=CARD, foreground=INK,
                    font=("Microsoft YaHei UI", 13, "bold"))
    style.configure("Device.TLabel", background=CARD, foreground=BRAND,
                    font=("Microsoft YaHei UI", 11, "bold"))
    style.configure("Caption.TLabel", background=CARD, foreground="#9aa1ad",
                    font=("Microsoft YaHei UI", 8, "bold"))
    style.configure("Arrow.TLabel", background=CARD, foreground="#c5ccd8",
                    font=("Microsoft YaHei UI", 12))
    style.configure("Profile.TCombobox", fieldbackground=BRAND_LIGHT,
                    background=BRAND_LIGHT, foreground=INK, bordercolor=BRAND_LIGHT,
                    lightcolor=BRAND_LIGHT, darkcolor=BRAND_LIGHT, arrowcolor=BRAND,
                    arrowsize=16, padding=(12, 5), relief="flat",
                    font=("Microsoft YaHei UI", 12, "bold"))
    style.map("Profile.TCombobox",
              fieldbackground=[("readonly", BRAND_LIGHT), ("active", "#e6e0ff")],
              background=[("readonly", BRAND_LIGHT), ("active", "#e6e0ff")],
              foreground=[("readonly", INK)],
              selectbackground=[("readonly", BRAND_LIGHT)],
              selectforeground=[("readonly", INK)],
              bordercolor=[("focus", "#d6c9ff"), ("!focus", BRAND_LIGHT)])
    style.configure("Muted.TLabel", background=BG, foreground=MUTED,
                    font=("Microsoft YaHei UI", 9))
    style.configure("Status.TLabel", background=BG, foreground=MUTED,
                    font=("Microsoft YaHei UI", 9))
    style.configure(
        "Treeview", background=CARD, fieldbackground=CARD, foreground=INK,
        rowheight=28, borderwidth=0, font=font,
    )
    style.map("Treeview",
              background=[("selected", BRAND_LIGHT)],
              foreground=[("selected", INK)])
    style.configure("Treeview.Heading", background="#f2f3f8", foreground=MUTED,
                    relief="flat", font=("Microsoft YaHei UI", 9, "bold"), padding=(8, 7))
    style.map("Treeview.Heading", background=[("active", "#e9ebf5")])
    style.configure("Primary.TButton", background=BRAND, foreground="#ffffff",
                    borderwidth=0, focusthickness=0, padding=(14, 7),
                    font=("Microsoft YaHei UI", 10, "bold"))
    style.map("Primary.TButton",
              background=[("active", BRAND_HOVER), ("pressed", "#5c3ce0")],
              foreground=[("disabled", "#ffffff")])
    style.configure("Ghost.TButton", background="#eef0f7", foreground=INK,
                    borderwidth=0, focusthickness=0, padding=(12, 6), font=font)
    style.map("Ghost.TButton", background=[("active", "#e2e6f3")])
    style.configure("TCombobox", padding=5, arrowsize=14)
    style.configure("Search.TEntry", fieldbackground=CARD, bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE, padding=(10, 6), relief="flat")
    style.map("Search.TEntry", bordercolor=[("focus", BRAND)])

    # ---------------------------------------------------------------- 顶部卡片
    header_wrap = tk.Frame(window, bg=BG)
    header_wrap.pack(fill="x", padx=14, pady=(14, 10))
    card = tk.Frame(header_wrap, bg=CARD, highlightbackground=LINE, highlightthickness=1)
    card.pack(fill="x")
    card.grid_columnconfigure(0, weight=1)

    left = ttk.Frame(card, style="Card.TFrame", padding=(18, 11, 8, 13))
    left.grid(row=0, column=0, sticky="w")

    profile_var = tk.StringVar(value=initial)
    ttk.Label(left, text="当前 PROFILE", style="Caption.TLabel").grid(
        row=0, column=0, sticky="w"
    )
    ttk.Label(left, text="切换到这个设备", style="Caption.TLabel").grid(
        row=0, column=2, sticky="w"
    )

    combo = ttk.Combobox(left, textvariable=profile_var, values=profiles,
                         state="readonly", width=9, style="Profile.TCombobox")
    combo.grid(row=1, column=0, sticky="w", pady=(3, 0))
    ttk.Label(left, text="→", style="Arrow.TLabel").grid(
        row=1, column=1, padx=12, pady=(3, 0)
    )
    device_label = tk.StringVar(value="")
    ttk.Label(left, textvariable=device_label, style="Device.TLabel").grid(
        row=1, column=2, sticky="w", pady=(3, 0)
    )

    right = ttk.Frame(card, style="Card.TFrame", padding=(8, 12, 18, 12))
    right.grid(row=0, column=1, sticky="e")

    # ---------------------------------------------------------------- 搜索栏
    search_bar = ttk.Frame(window, padding=(14, 0, 14, 10))
    search_bar.pack(fill="x")
    ttk.Label(search_bar, text="搜索", style="Muted.TLabel").pack(side="left", padx=(2, 8))
    query_var = tk.StringVar()
    search_entry = ttk.Entry(search_bar, textvariable=query_var, width=30,
                             style="Search.TEntry")
    search_entry.pack(side="left")
    ttk.Label(search_bar, text="   程序名 / 进程名；输入 xxx.exe 回车直接添加",
              style="Muted.TLabel").pack(side="left")

    # ---------------------------------------------------------------- 列表
    table_wrap = ttk.Frame(window, padding=(14, 0, 14, 6))
    table_wrap.pack(fill="both", expand=True)

    columns = ("app", "exe", "run", "state")
    tree = ttk.Treeview(table_wrap, columns=columns, show="tree headings",
                        selectmode="browse")
    tree.heading("#0", text="")
    tree.column("#0", width=32, minwidth=32, anchor="center", stretch=False)
    tree.heading("app", text="程序")
    tree.heading("exe", text="进程名")
    tree.heading("run", text="运行")
    tree.heading("state", text=f"规则（单击切换）")
    tree.column("app", width=320, anchor="w", stretch=True)
    tree.column("exe", width=200, anchor="w")
    tree.column("run", width=76, anchor="center")
    tree.column("state", width=130, anchor="center")

    scroll = ttk.Scrollbar(table_wrap, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    tree.tag_configure("out", foreground="#78808f")

    # ---------------------------------------------------------------- 底部
    ttk.Separator(window, orient="horizontal").pack(fill="x", padx=14, pady=(2, 0))
    footer = ttk.Frame(window, padding=(14, 8, 14, 12))
    footer.pack(fill="x")
    status_var = tk.StringVar(value="")
    status_label = ttk.Label(footer, textvariable=status_var, style="Status.TLabel")
    status_label.pack(side="left")

    def say(text: str, kind: str = "info") -> None:
        colors = {"info": MUTED, "ok": GREEN, "err": RED, "warn": AMBER}
        status_label.configure(foreground=colors.get(kind, MUTED))
        status_var.set(text)

    def endpoints_map() -> dict[str, str]:
        try:
            return {ep.id.lower(): ep.label for ep in dv.enum_endpoints()}
        except Exception:  # noqa: BLE001
            return {}

    # ---------------------------------------------------------------- 逻辑
    icon_cache: dict[str, object] = {}

    def photo_for(exe: str, pid: int | None = None):
        """exe -> tk.PhotoImage（带缓存）；失败返回 None，表格就不显示图标。"""
        key = exe.lower()
        if key in icon_cache:
            return icon_cache[key]
        photo = None
        png = appicon.icon_png(exe, pid=pid)
        if png:
            try:
                photo = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
            except tk.TclError:
                photo = None
        icon_cache[key] = photo
        return photo

    def rules_of(prof: str) -> set[str]:
        return {e.lower() for r in cfg.rules if r.profile == prof for e in r.exe}

    def select_exe(exe: str) -> None:
        for item in tree.get_children():
            if str(tree.item(item, "values")[1]).lower() == exe.lower():
                tree.selection_set(item)
                tree.see(item)
                return

    def reload_cfg() -> None:
        fresh = cfgmod.Config.load(cfg.path)
        cfg.rules = fresh.rules
        cfg.profiles = fresh.profiles

    def notify_daemon(message: str) -> str:
        if ipc.signal(ipc.RELOAD_EVENT):
            return message + "　→　守护进程已生效"
        if not ipc.daemon_running():
            return message + "　（守护进程没在跑，下次启动生效）"
        return message

    def refresh(*, keep_selection: bool = False) -> None:
        selected = ""
        if keep_selection and tree.selection():
            selected = str(tree.item(tree.selection()[0], "values")[1])

        for row in tree.get_children():
            tree.delete(row)

        profile = profile_var.get()
        current = rules_of(profile)
        query = query_var.get().strip().lower()
        apps = visible_apps()
        running = {exe for _t, exe, _p in apps}
        known = appnames.cached_keys()
        device_label.set(_device_label(cfg, profile, endpoints_map()))

        def hit(*texts: str) -> bool:
            return (not query) or any(query in (t or "").lower() for t in texts)

        shown = 0
        for title, exe, pid in apps:
            if exe not in known:
                appnames.display_name(exe, pid=pid)  # 顺便写入名称缓存
            wanted = exe in current
            if not hit(title, exe):
                continue
            shown += 1
            tree.insert(
                "", "end",
                image=photo_for(exe, pid),
                values=(f"  {title}", exe, "运行中", "✓ 已添加" if wanted else "＋ 未添加"),
                tags=() if wanted else ("out",),
            )

        idle = sorted(key for key in current if key not in running)
        for key in idle:
            name = appnames.display_name(key) or appnames.pretty(key)
            if not hit(name, key):
                continue
            shown += 1
            tree.insert(
                "", "end",
                image=photo_for(key),
                values=(f"  {name}", key, "未运行", "✓ 已添加"),
            )

        if shown == 0:
            if query:
                hint = "按回车直接添加" if query.endswith(".exe") else "换个关键词试试"
                say(f"没有匹配「{query_var.get().strip()}」的程序，{hint}", "warn")
            else:
                say(f"[{profile}] 还没有任何规则，点一下程序右侧的「＋ 未添加」即可", "warn")
        elif not current:
            say(
                f"[{profile}] 还没有任何规则 —— 点一下右侧的「＋ 未添加」即可添加",
                "warn",
            )
        else:
            say(
                f"[{profile}] {len(current)} 个规则 · {len(apps)} 个程序在运行 · "
                f"显示 {shown} 行",
                "info",
            )

        if selected:
            select_exe(selected)

    def add_exe(exe: str, title: str = "") -> None:
        profile = profile_var.get()
        try:
            added, message = cfgmod.add_exe_rule(exe, profile, cfg.path, move=True)
        except Exception as exc:  # noqa: BLE001
            say(f"✗ {exc}", "err")
            return
        reload_cfg()
        query_var.set("")
        refresh()
        select_exe(exe)
        if added:
            name = title or appnames.display_name(exe) or appnames.pretty(exe)
            say("✓ " + notify_daemon(f"已加入 {profile}：{name}"), "ok")
        else:
            say("• " + message, "info")

    def remove_exe(exe: str) -> None:
        profile = profile_var.get()
        try:
            removed, message = cfgmod.remove_exe_rule(exe, profile, cfg.path)
        except Exception as exc:  # noqa: BLE001
            say(f"✗ {exc}", "err")
            return
        reload_cfg()
        refresh()
        if removed:
            say("✓ " + notify_daemon(f"已从 {profile} 移除：{exe}"), "ok")
        else:
            say("• " + message, "info")

    def toggle_row(row: str | None) -> None:
        if not row:
            return
        values = tree.item(row, "values")
        exe = str(values[1])
        if str(values[3]).startswith("✓"):
            remove_exe(exe)
        else:
            add_exe(exe, title=str(values[0]).strip())

    def grab_foreground() -> None:
        app = foreground_app()
        if not app:
            say("找不到前台窗口", "err")
            return
        title, exe, _pid = app
        if exe in NOT_GAME_EXES:
            say(f"「{title}」看起来不是游戏；确需添加请用「浏览 exe 文件」", "warn")
            return
        add_exe(exe, title=title)

    def browse() -> None:
        path = filedialog.askopenfilename(
            parent=window,
            title="选择要自动切换的程序",
            filetypes=[("程序", "*.exe"), ("快捷方式", "*.lnk"), ("所有文件", "*.*")],
        )
        if not path:
            return
        resolved = resolve_shortcut(Path(path))
        if resolved is None:
            say("✗ 无法解析这个快捷方式（可能指向了不存在的文件）", "err")
            return
        add_exe(resolved.name)

    def toggle_selected(_event=None) -> None:
        selection = tree.selection()
        if selection:
            toggle_row(selection[0])
            return
        text = query_var.get().strip()
        if text.lower().endswith(".exe"):
            add_exe(text)
        else:
            say("先在列表里选一个程序；或输入 xxx.exe 后回车直接添加", "warn")

    # ---------------------------------------------------------------- 按钮 & 事件
    ttk.Button(right, text="＋ 收录当前程序", style="Primary.TButton",
               command=grab_foreground).pack()

    ttk.Button(footer, text="浏览 exe 文件…", style="Ghost.TButton",
               command=browse).pack(side="right")
    ttk.Button(footer, text="刷新列表", style="Ghost.TButton",
               command=lambda: refresh(keep_selection=True)).pack(side="right", padx=(0, 8))
    ttk.Button(footer, text="关闭", style="Ghost.TButton",
               command=window.destroy).pack(side="right", padx=(0, 8))

    def on_single_click(event) -> None:
        if tree.identify("region", event.x, event.y) not in ("cell", "tree"):
            return
        if tree.identify_column(event.x) == "#4":
            toggle_row(tree.identify_row(event.y))

    def on_double_click(event) -> None:
        if tree.identify_column(event.x) == "#4":
            return
        toggle_row(tree.identify_row(event.y))

    tree.bind("<Button-1>", on_single_click, add="+")
    tree.bind("<Double-1>", on_double_click, add="+")
    tree.bind("<Return>", toggle_selected)
    tree.bind("<Delete>", lambda _e: (
        remove_exe(str(tree.item(tree.selection()[0], "values")[1]))
        if tree.selection() else None
    ))
    query_var.trace_add("write", lambda *_: refresh(keep_selection=True))
    search_entry.bind("<Return>", toggle_selected)

    def on_profile_change(_event=None) -> None:
        refresh()
        say(f"已切到 {profile_var.get()}", "info")

    combo.bind("<<ComboboxSelected>>", on_profile_change)

    def on_close() -> None:
        _save_ui_state({"profile": profile_var.get(), "geometry": window.geometry()})
        window.destroy()

    window.protocol("WM_DELETE_WINDOW", on_close)
    window.bind("<Escape>", lambda _e: on_close())

    # ---------------------------------------------------------------- 显示
    geometry = ui_state.get("geometry")
    if isinstance(geometry, str) and "x" in geometry:
        try:
            window.geometry(geometry)
        except tk.TclError:
            geometry = None
    else:
        geometry = None

    refresh()

    if geometry is None:
        window.update_idletasks()
        width, height = 820, 560
        x = (window.winfo_screenwidth() - width) // 2
        y = max(0, (window.winfo_screenheight() - height) // 3)
        window.geometry(f"{width}x{height}+{x}+{y}")
    window.lift()
    window.focus_force()
    search_entry.focus_set()

    window.mainloop()
    return 0
