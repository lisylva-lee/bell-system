# 🔔 智能打铃系统 BellSystem

## 项目结构

```
BellSystem/
├── BellSystem.exe          ← 编译好的主程序 (双击运行)
├── bell_system.py          ← 源码
├── bell.ico                ← 程序图标
├── make_icon.py            ← 图标生成脚本
├── BellSystem.spec         ← PyInstaller 配置文件
│
├── data/                   ← 数据库 (自动创建)
│   └── bell.db             ← SQLite 数据库
│
├── logs/                   ← 日志 (自动创建)
│   └── 2026-08-19.log
│
├── config/                 ← 配置 (自动创建)
│   └── settings.json
│
├── ffmpeg/                 ← 可选：放 ffmpeg.exe 支持更多音频格式
└── dist/                   ← 编译输出目录
    └── BellSystem.exe
```

## 功能

| 功能 | 说明 |
|------|------|
| **定时方式** | 每天 / 每周(选星期) / 指定日期 / 一次性 |
| **音频格式** | MP3, WAV, M4A, AAC, OGG, FLAC (通过 pygame) |
| **铃声方案** | 多套方案一键切换 (如：学校工作日 / 工厂 / 自定义) |
| **系统托盘** | 右下角托盘运行，右键菜单快速操作 |
| **执行日志** | 每次打铃自动记录，查看历史执行情况 |
| **开机自启** | 设置中可启用 |
| **防止休眠** | 设置中可启用 |
| **绿色免安装** | 解压即用，无需安装 |

## 使用方法

1. 双击 `BellSystem.exe` 运行
2. 点击 **添加任务** 设置打铃规则
3. 选择铃声文件（MP3/WAV 等）
4. 设置重复方式和时间
5. 关闭窗口自动最小化到系统托盘

## 编译说明

已在当前系统编译好 EXE 文件：`dist/BellSystem.exe`

如需重新编译：
```bash
pip install pyinstaller pygame pystray pillow
pyinstaller --onefile --windowed --icon=bell.ico --name="BellSystem" bell_system.py
```

## 系统要求

- Windows 7 / 10 / 11
- 无需安装 .NET Framework
- 无需安装 Python