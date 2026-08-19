# 🔔 智能打铃系统 BellSystem

> Windows 7/10/11 通用的本地定时打铃软件 — 不依赖服务器、不依赖网络，绿色免安装。

**GitHub 仓库**: https://github.com/lisylva-lee/bell-system
（push 到 `main` 分支自动触发 Actions 双架构编译，产物在 Actions Artifacts 下载）

## 项目结构

```
BellSystem/
├── BellSystem_Win7_x86.exe ← 32位版（Win7 32/64位通用）
├── BellSystem_x64.exe      ← 64位版（Win10/11 64位）
├── bell_system.py          ← 源码
├── bell.ico                ← 程序图标
├── .github/workflows/      ← CI 自动编译配置
│
├── data/                   ← SQLite 数据库（自动创建，与 exe 同目录）
│   └── bell.db
├── logs/                   ← 运行日志（自动创建）
├── config/                 ← 配置文件（自动创建）
└── ffmpeg/                 ← 可选：放 ffmpeg.exe 支持更多音频格式
```

## 功能

| 功能 | 说明 |
|------|------|
| **定时方式** | 每天 / 每周(选星期) / 指定日期 / 一次性 |
| **音频格式** | MP3, WAV, M4A, AAC, OGG, FLAC |
| **铃声方案** | 多套方案一键切换（学校工作日 / 工厂 / 自定义） |
| **系统托盘** | 右下角托盘运行，右键菜单快速操作 |
| **执行日志** | 每次打铃自动记录成功/失败原因 |
| **主界面** | 当前时间、精确倒计时（今天/明天/日期+星期）、任务列表 |
| **开机自启** | 设置中可启用（注册表） |
| **防止休眠** | 设置中可启用（电源策略） |
| **绿色免安装** | 解压即用，数据持久化在 exe 同目录 |

## 使用方法

1. 双击 `BellSystem.exe` 运行
2. 点击 **添加任务** 设置打铃规则（任务名、重复方式、时间、铃声、音量）
3. 关闭窗口自动最小化到系统托盘，到点自动打铃

## 编译说明

### GitHub Actions（推荐）

push 到 `main` 自动编译 x86/x64 两个版本，产物在仓库 Actions 页面的 Artifacts：

```yaml
# .github/workflows/build.yml 核心
- uses: actions/setup-python@v5
  with:
    python-version: '3.8.10'      # 最后一个官方支持 Win7 的版本
    architecture: ${{ matrix.arch }}  # x86 / x64
- run: pip install pygame==2.5.2 pystray pillow pyinstaller
- run: pyinstaller --onefile --windowed --icon=bell.ico --name=BellSystem bell_system.py
```

### 本地编译

```bash
pip install pyinstaller pygame pystray pillow
pyinstaller --onefile --windowed --icon=bell.ico --name="BellSystem" \
  --hidden-import=pygame --hidden-import=pystray --hidden-import=PIL bell_system.py
```

> ⚠️ 注意：编译 32 位版必须使用 32 位 Python。
> Win11 上无法安装 Python 3.8.10（WiX 安装器 1603 bug），建议直接用 GitHub Actions 编译。

## 系统要求

- Windows 7（32/64 位）/ 10 / 11
- 无需安装 .NET / Python，绿色运行
- 数据目录（data/）需有写入权限；放 U 盘、共享盘均可

## 版本历史

### v1.1 (2026-08-19)
- 🐛 修复：连续多个任务（如 14:13/14:14/14:15）到点瞬间被重算逻辑推出，导致后续任务静默丢失
- 🐛 修复：onefile 打包后数据库写入临时目录，关闭软件后任务全部消失 → 数据持久化到 exe 同目录
- ✅ 新增：GitHub Actions 双架构（x86/x64）自动编译
- ✅ 精确倒计时：正确显示多天后任务的距离（不再假设当天/明天）

### v1.0 (2026-08-19)
- 初始版本：定时打铃、铃声管理、系统托盘、方案切换、日志