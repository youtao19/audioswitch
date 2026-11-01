"""把 exe 进程名翻译成人类可读的程序名。

来源（按优先级）：
  1. 正在运行的进程 → 进程 exe 路径 → VERSIONINFO 的 FileDescription
  2. 名称缓存 app_names.json（用过一次就记住）
  3. Steam 库（appmanifest_*.acf 里的游戏名 + common/<installdir>/*.exe）
  4. 注册表：App Paths / Uninstall 的 DisplayName（抖音这类向导式安装的程序）
  5. 都没有 → 返回空串，由调用方用 exe 文件名兜底
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import winreg
from ctypes import byref, wintypes
from pathlib import Path

log = logging.getLogger("aswitch")

ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = ROOT / "app_names.json"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

version = ctypes.WinDLL("version", use_last_error=True)
version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
version.GetFileVersionInfoW.restype = wintypes.BOOL
version.VerQueryValueW.argtypes = [
    ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)
]
version.VerQueryValueW.restype = wintypes.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 内置常见程序名（本机规则里的兜底；可以在 app_names.json 里覆盖）
KNOWN_NAMES = {
    "cs2.exe": "Counter-Strike 2",
    "csgo.exe": "CS:GO",
    "valorant-win64-shipping.exe": "无畏契约",
    "valauncher.exe": "无畏契约（启动器）",
    "league of legends.exe": "英雄联盟",
    "dota2.exe": "Dota 2",
    "tslgame.exe": "绝地求生 PUBG",
    "r5apex.exe": "Apex 英雄",
    "overwatch.exe": "守望先锋",
    "genshinimpact.exe": "原神（国际服）",
    "yuanshen.exe": "原神",
    "starrail.exe": "崩坏：星穹铁道",
    "eldenring.exe": "艾尔登法环",
    "narakabladepoint.exe": "永劫无间",
    "gta5.exe": "GTA V",
    "douyin.exe": "抖音",
}

_cache: dict[str, dict] | None = None
_registry: dict[str, tuple[str, str]] | None = None
_steam: dict[str, tuple[str, str]] | None = None


# ---------------------------------------------------------------- 版本信息
def file_description(path: str | os.PathLike) -> str:
    """读 exe 的 VERSIONINFO：先 FileDescription，再 ProductName。"""
    path = str(path)
    try:
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, buf):
            return ""
    except Exception:  # noqa: BLE001
        return ""

    ptr = ctypes.c_void_p()
    length = wintypes.UINT()
    pairs = [(0x0409, 0x04B0)]
    if version.VerQueryValueW(buf, "\\VarFileInfo\\Translation", byref(ptr), byref(length)) and length.value >= 4:
        words = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD))
        pairs.insert(0, (words[0], words[1]))

    for lang, codepage in pairs:
        for key in ("FileDescription", "ProductName"):
            sub = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{key}"
            if version.VerQueryValueW(buf, sub, byref(ptr), byref(length)) and length.value:
                text = ctypes.wstring_at(ptr, length.value).rstrip("\x00").strip()
                if text:
                    return text
    return ""


def process_path(pid: int) -> str:
    """拿进程的 exe 完整路径（无需管理员）。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------- 缓存
def _load_cache() -> dict[str, dict]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if not isinstance(_cache, dict):
                _cache = {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def remember(exe: str, name: str, path: str | None = None) -> None:
    if not name:
        return
    key = exe.lower()
    cache = _load_cache()
    entry = {"name": name}
    if path:
        entry["path"] = path
    if cache.get(key) == entry:
        return
    cache[key] = entry
    try:
        CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        log.debug("写名称缓存失败", exc_info=True)


# ---------------------------------------------------------------- 注册表
def _expand(value) -> str | None:
    if not value:
        return None
    text = str(value).strip().strip('"')
    if text.lower().endswith(",0"):
        text = text[:-2]
    return os.path.expandvars(text)


def _registry_index() -> dict[str, tuple[str, str]]:
    """exe -> (显示名, 路径)。来自 App Paths 和卸载项里的 DisplayIcon。"""
    global _registry
    if _registry is not None:
        return _registry
    index: dict[str, tuple[str, str]] = {}

    app_path_roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    ]
    for root, sub in app_path_roots:
        try:
            with winreg.OpenKey(root, sub) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, name) as sub_key:
                            path = _expand(winreg.QueryValue(sub_key, None))
                    except OSError:
                        continue
                    if path and os.path.isfile(path):
                        index.setdefault(name.lower(), ("", path))
        except OSError:
            continue

    uninstall_roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for root, sub in uninstall_roots:
        try:
            with winreg.OpenKey(root, sub) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub_key:
                            try:
                                display = winreg.QueryValueEx(sub_key, "DisplayName")[0]
                            except OSError:
                                display = ""
                            try:
                                icon = winreg.QueryValueEx(sub_key, "DisplayIcon")[0]
                            except OSError:
                                icon = None
                    except OSError:
                        continue
                    path = _expand(icon)
                    if path and path.lower().endswith(".exe") and os.path.isfile(path):
                        index.setdefault(os.path.basename(path).lower(), (str(display or ""), path))
        except OSError:
            continue

    _registry = index
    return index


# ---------------------------------------------------------------- Steam
def _steam_roots() -> list[Path]:
    roots: list[Path] = []
    steam = None
    for root, sub, value in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(root, sub) as key:
                steam = winreg.QueryValueEx(key, value)[0]
            break
        except OSError:
            continue
    if steam:
        candidate = Path(str(steam).replace("/", "\\"))
        if candidate.is_dir():
            roots.append(candidate)
            vdf = candidate / "steamapps" / "libraryfolders.vdf"
            try:
                text = vdf.read_text(encoding="utf-8", errors="ignore")
                for match in re.finditer(r'"path"\s+"([^"]+)"', text):
                    lib = Path(match.group(1).replace("\\\\", "\\"))
                    if lib.is_dir() and lib not in roots:
                        roots.append(lib)
            except OSError:
                pass
    return roots


def _iter_exes(root: Path, max_depth: int = 6, limit: int = 600):
    queue = [(root, 0)]
    found = 0
    while queue and found < limit:
        current, depth = queue.pop(0)
        try:
            for entry in os.scandir(current):
                if entry.is_file() and entry.name.lower().endswith(".exe"):
                    found += 1
                    yield Path(entry.path)
                    if found >= limit:
                        return
                elif entry.is_dir() and depth < max_depth:
                    queue.append((Path(entry.path), depth + 1))
        except OSError:
            continue


def _steam_index() -> dict[str, tuple[str, str]]:
    """exe -> (游戏名, 路径)，来自 Steam 的 appmanifest + 安装目录。"""
    global _steam
    if _steam is not None:
        return _steam
    index: dict[str, tuple[str, str]] = {}
    for base in _steam_roots():
        apps_dir = base / "steamapps"
        if not apps_dir.is_dir():
            continue
        for manifest in apps_dir.glob("appmanifest_*.acf"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            name = re.search(r'"name"\s+"([^"]+)"', text)
            folder = re.search(r'"installdir"\s+"([^"]+)"', text)
            if not name or not folder:
                continue
            game_dir = apps_dir / "common" / folder.group(1)
            if not game_dir.is_dir():
                continue
            for exe in _iter_exes(game_dir):
                index.setdefault(exe.name.lower(), (name.group(1), str(exe)))
    _steam = index
    return index


# ---------------------------------------------------------------- 对外
def exe_path(exe: str, pid: int | None = None) -> str | None:
    """尽量找出 exe 的完整路径（用于取图标）。"""
    key = (exe or "").lower()
    if pid:
        path = process_path(pid)
        if path:
            return path
    cached = (_load_cache().get(key) or {}).get("path")
    if cached:
        return str(cached)
    hit = _steam_index().get(key)
    if hit and hit[1]:
        return hit[1]
    hit = _registry_index().get(key)
    if hit and hit[1]:
        return hit[1]
    return None


def display_name(exe: str, pid: int | None = None) -> str:
    """返回人类可读的程序名；找不到时返回空串（调用方自己兜底）。"""
    key = (exe or "").lower()
    if not key:
        return ""

    cache = _load_cache()

    # 1) 运行中的进程：直接拿路径读版本信息（最准）
    if pid:
        path = process_path(pid)
        if path:
            name = file_description(path)
            if name:
                remember(key, name, path)
                return name

    # 2) 缓存
    cached = cache.get(key) or {}
    if cached.get("name"):
        return str(cached["name"])

    # 3) Steam 库
    hit = _steam_index().get(key)
    if hit:
        remember(key, hit[0], hit[1])
        return hit[0]

    # 4) 注册表（App Paths / 卸载项）—— DisplayName 通常比 VERSIONINFO 更友好（如「抖音」）
    hit = _registry_index().get(key)
    if hit:
        display, path = hit
        name = display or file_description(path)
        if name:
            remember(key, name, path)
            return name

    # 5) 内置常见名字
    if key in KNOWN_NAMES:
        return KNOWN_NAMES[key]
    return ""


def cached_keys() -> set[str]:
    return set(_load_cache())


def pretty(exe: str) -> str:
    """兜底：valorant-win64-shipping.exe -> Valorant Win64 Shipping"""
    stem = (exe or "").rsplit(".", 1)[0]
    words = [w for w in re.split(r"[-_\s]+", stem) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)


if __name__ == "__main__":  # 手动调试用
    import sys

    for arg in sys.argv[1:] or ["cs2.exe", "douyin.exe", "dota2.exe"]:
        print(f"{arg:<28} -> {display_name(arg) or '(未知)'}")
