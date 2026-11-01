# audioswitch

Windows 音频设备快捷切换 / 自动切换工具。

* 热键一键切换（音响 ↔ 耳机）
* 托盘菜单切换
* **启动游戏自动切到耳机，游戏退出自动切回音响**
* 同时切换「播放默认设备」和「录音默认设备」，并且三个角色（console / multimedia / communications）一起切
* 纯 Python 标准库实现，零第三方依赖，不需要管理员权限

## 快速开始

```bat
cd C:\Users\youtao\audioswitch

aswitch.cmd init          :: 1. 按当前设备生成 config.toml
aswitch.cmd list          :: 2. 核对设备别名
aswitch.cmd use headset   :: 3. 手动切一次，确认声音去了耳机
aswitch.cmd use speaker   :: 4. 再切回来
aswitch.cmd start         :: 5. 后台启动托盘 + 热键 + 自动切换
aswitch.cmd status        :: 6. 随时确认它在不在跑
```

> 等价写法：`python -m aswitch <命令>`。如果觉得 `aswitch.cmd` 好用，可以把这个目录加进 PATH。

后台常驻：`aswitch start`（或双击 `aswitch-daemon.vbs`，无黑窗口）。
退出：托盘右键 → 退出，或 `aswitch quit`。

### 开机自启

`aswitch-daemon.vbs` 的快捷方式放进 `Win+R` → `shell:startup` 即可。
（本机已经装好了：`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\audioswitch.lnk`，
不想要就删掉这个快捷方式。）

## 桌面快捷方式

`assets/make-shortcuts.ps1` 会创建两个桌面快捷方式（本机已创建）：

| 快捷方式 | 作用 |
|---|---|
| `切换音频设备` | 双击 = 在两个 profile 间 `toggle`，不弹窗 |
| `audioswitch 命令` | 开控制台：先显示当前状态 + 命令速查，然后可继续敲命令 |

删了想重建：

```bat
powershell -ExecutionPolicy Bypass -File assets\make-shortcuts.ps1
```

> 批处理（`.cmd`）一律只写 ASCII：cmd.exe 按 OEM 代码页（中文系统 = GBK）读 `.cmd`，
> 存成 UTF-8 中文会被拆成乱码、甚至把命令拆断。中文提示全部交给 Python 输出（`aswitch help`）。

## 命令

| 命令 | 说明 |
|---|---|
| `aswitch list [-a] [--json]` | 列出设备。`-a` 包含未连接/禁用的 |
| `aswitch status` | 当前每个角色用的是哪个设备 |
| `aswitch use <profile>` | 切换到某个 profile |
| `aswitch toggle` | 在 `[settings] toggle` 的两个 profile 间切换 |
| `aswitch init [-f]` | 生成/覆盖配置模板 |
| `aswitch launch --profile headset -- "C:\game\game.exe"` | 先切设备再启动程序 |
| `aswitch start` | 后台启动守护进程（不占终端） |
| `aswitch quit` | 让守护进程退出 |
| `aswitch daemon [--no-tray] [--no-hotkeys] [--no-watch] [--dry-run]` | 前台运行守护进程（调试用） |
| `aswitch watch` | 只打印规则命中情况，不切设备（调 exe 名用） |
| `aswitch config [--open]` | 配置文件路径 / 打开它 |

## 配置

`config.toml`，默认在项目根目录；可用环境变量 `ASWITCH_CONFIG` 指到别处。

```toml
[settings]
poll_interval = 1.0    # 进程轮询间隔（秒）
restore_delay = 3.0    # 规则进程全退出后延迟多久还原（防止切图/启动器抖动）
notify = true          # 切换时弹托盘气泡
toggle = ["headset", "speaker"]

[render]               # 播放设备别名 -> 端点 ID
spk = "{0.0.0.00000000}.{...}"
hp  = "{0.0.0.00000000}.{...}"

[capture]              # 录音设备别名 -> 端点 ID（可以和上面同名，指向不同端点）
hp  = "{0.0.1.00000000}.{...}"

[profiles.headset]
render = "hp"
capture = "hp"
# comm_render = "hp"   # 可选：单独指定"通信设备"（Discord / 游戏语音用这个）
# comm_capture = "hp"

[profiles.speaker]
render = "spk"

[[rules]]              # 命中任一条 exe 就切到 profile
exe = ["cs2.exe", "valorant-win64-shipping.exe"]
profile = "headset"
enabled = true

[hotkeys]
"ctrl+alt+1" = "headset"   # 值可以是 profile 名，或 "toggle"
"ctrl+alt+2" = "speaker"
"ctrl+alt+3" = "toggle"
```

> 全局热键是先到先得：如果日志里出现 `注册热键 xxx 失败（1409）`，说明那个组合已被别的程序占了。
> 本机 `Alt+F1/F2/F3/F9/F10` 属 NVIDIA App（截图 / 录制 / 即时重放），在 NVIDIA App → 设置 → 快捷键里改掉，
> 再点托盘「重载配置」即可。

要点：

* profile 名可以随便取（本机用的是 `耳机` / `音响`，中文名要加引号）。改 profile 名时记得同步改三处：
  `[settings] toggle`、`[[rules]] profile`、`[hotkeys]` 的值。
* **别名指向端点 ID，不要改成设备名**。名字会随驱动/语言变，ID 不会。
* `exe` 写**进程名**（不是路径），支持通配符，如 `"*shipping.exe"`。不知道真名就跑 `aswitch watch`，启动游戏后看输出。
* 同一台设备同时有播放和录音端点时，两个表里可以用同一个别名，程序会按上下文取对的那个。
* 多条规则同时命中时，**按配置顺序**第一条生效。

## 工作原理

| 环节 | 做法 | 为什么 |
|---|---|---|
| 切换默认设备 | 未文档化 COM 接口 `IPolicyConfig::SetDefaultEndpoint`（CLSID `CPolicyConfigClient`） | Windows 没有公开 API；所有同类工具都走这个接口 |
| 三个角色 | 每个设备设 `eConsole` / `eMultimedia` / `eCommunications` | 只设前两个的话，Discord、游戏语音不会跟着切 |
| 设备枚举 | `IMMDeviceEnumerator` + `IPropertyStore` 读 `PKEY_Device_FriendlyName` | 拿到稳定端点 ID 和状态（active / unplugged） |
| 进程检测 | Toolhelp32 快照轮询（默认 1s） | 无依赖、开销极小、不会被反作弊关注 |
| 热键 | `RegisterHotKey` | **不用键盘钩子**，不注入进程，Vanguard / EAC 友好 |
| 托盘 | `Shell_NotifyIcon` | 无依赖 |

反作弊提示：本工具不注入任何进程、不读写游戏内存，只用系统级热键注册和进程列表。如果仍担心，`aswitch daemon --no-hotkeys` 只保留托盘 + 自动切换，或用 `--dry-run` 观察行为。

## 常见问题

**开了游戏没自动切换**
90% 是守护进程没在跑。先看 `aswitch status` 第一行：

```
守护进程: 未运行 ❌  ->  aswitch start
```

守护进程在跑时，`status` 还会告诉你「当前命中的规则」，一眼看出是规则没配上还是进程名不对。

**守护进程要怎么启动/停止**
`aswitch start` / `aswitch quit`，或双击 `aswitch-daemon.vbs` / 托盘右键退出。
（`aswitch daemon` 是前台运行，占着终端；平时用 `start`。）

**切了但游戏没声音 / 还是从音响出**
部分游戏（WASAPI 独占、Unity/UE 启动时锁设备）在初始化时就把设备定死了。用 `aswitch launch --profile headset -- "game.exe"` **先切再启动**。

**耳机没插的时候切不过去**
程序会先检查目标设备状态，不在 `active` 就不切（否则会直接静音），守护进程只在日志里警告一次。插上耳机后执行一次 `aswitch use headset` 即可。

**游戏退出后没还原**
`restore_delay` 是延迟秒数；另外如果手动按过热键，退出时仍会按进入游戏前的快照还原。想改成"永远回音响"，把规则里的 profile 换成一个固定 profile，或者把 `restore_delay` 调小。

**热键没反应**
可能被别的程序占用（错误码 1409），换一个组合；`--verbose` 看日志。游戏以管理员身份运行时，非管理员的 `RegisterHotKey` 有时收不到，此时把守护进程也以管理员启动。

**日志**
`aswitch.log`（项目根目录），`--verbose` 输出 DEBUG。

## 目录结构

```
aswitch/        包
  com.py        ctypes COM 基础设施
  devices.py    端点枚举 / 默认设备查询
  policy.py     IPolicyConfig 封装（真正的切换）
  config.py     config.toml 读写 + 首次生成
  engine.py     profile 应用 / 快照还原 / toggle
  watcher.py    进程轮询 + 规则状态机
  hotkey.py     RegisterHotKey + 消息循环
  tray.py       托盘图标与菜单
  cli.py        命令行
aswitch.cmd             启动器
aswitch-daemon.vbs      静默启动（用于开机自启）
assets/aswitch.png      图标源图（1254×1254 透明 PNG）
assets/aswitch.ico      多尺寸图标 16/20/24/32/40/48/64/128/256
assets/make-ico.ps1     PNG -> 多尺寸 ICO（纯 PowerShell，无需 Pillow）
assets/preview-ico.ps1  把 ICO 各尺寸放大成 PNG，用来检查小尺寸可读性
assets/make-shortcuts.ps1  重建两个桌面快捷方式
docs/usage.html         图文版使用说明（单文件 HTML，图标已内嵌）
config.toml             你的配置
```

## 图标

托盘图标读的是 `assets/aswitch.ico`，按当前 DPI 取对应尺寸（100% 用 16px，125% 用 20px，150% 用 24px）。
文件不存在时自动回退到系统默认图标，不会报错。

换图标：把自己的 PNG 存成 `assets/aswitch.png`，然后

```bat
cd assets
powershell -ExecutionPolicy Bypass -File make-ico.ps1 -Source aswitch.png -Output aswitch.ico
powershell -ExecutionPolicy Bypass -File preview-ico.ps1 -Ico aswitch.ico   :: 可选：检查 16px 效果
aswitch.cmd quit && aswitch.cmd start                                     :: 重启托盘
```

脚本会按 alpha 通道自动裁掉多余留白（留 4%），让小尺寸下的图形更饱满。
