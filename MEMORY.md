# MEMORY — BellSystem 智能打铃系统

> 项目记忆文件 · 更新于 2026-08-19

## 项目索引

- **位置**: `F:\xiangmu\hermes\music\`
- **GitHub**: https://github.com/lisylva-lee/bell-system （`main` 分支推代码自动触发 CI 编译）
- **Ci workflow**: `.github/workflows/build.yml` — windows-latest + Python 3.8.10 × {x86, x64} 双架构
- **技术栈**: Python 3.8 + tkinter + pygame(2.5.2) + pystray + Pillow + SQLite + PyInstaller --onefile
- **产物**: `dist/BellSystem_Win7_x86.exe` (32位 Win7) / `dist/BellSystem_x64.exe` (64位)
- **运行环境**: 无依赖、绿色免安装；数据存 exe 同目录 data/、logs/、config/

## 关键决策 / 踩坑记录

### 1. 为什么用 GitHub Actions 编译
本地 Win11 无法安装 Python 3.8.10 32 位（WiX burn 引擎 1603 bug，`/layout` 只解出 `_d` 调试版 MSI，ALLUSERS=0/1 均失败）。改用 CI：runner 无此问题。⚠️ 本地 python.org/pypi 直连慢，走代理 `http://10.168.1.245:17890`（系统代理，curl/gh 需显式 `-x` 或 `HTTPS_PROXY` 环境变量）。

### 2. 必须 exe 目录做数据根（持久化）
`--onefile` 下 `__file__` 指向临时 `_MEIxxxxx` 目录，数据库/日志会随退出丢失。已修：
```python
if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).resolve().parent
```

### 3. Scheduler 重算保护（核心 bug）
`waited % 5 == 0` 重算若在**到点瞬间**触发：daily 任务 `t <= now` 被推到明天 → `_calculate_next` 返回的是**下一个不同 id 任务** → 误判 REPLAN → 白等一轮，表现为"只有第一个任务响，后续任务消失"。
**修复**：
- 距目标 ≤6 秒跳过重算（`continue`）
- 重算比较用 `task2["id"] == self._next_task_id`（同一任务时间滚动不算变化）

### 4. REPLAN 语义
`scheduler._next_dt/_next_task_id` 保存当前等待任务；`get_next_info()` 返回 (datetime, name) 供 UI 精确倒计时（修复"距离显示错误"——不再假设当天/明天）。

## 调度逻辑（每日）
```
计算下次 → 更新显示(_next_dt) → 逐秒等待 → 每5s重算(带保护) → 到点±5s执行 → 循环
daily: t=now.replace(h:m:s) 若 t<=now 则 +1天
weekly: 14天内找首个匹配星期
date/once: 指定日期，once 执行后自删
```

## 待办 / 后续
- [ ] V1.1: 铃声方案音量/播放设备选择
- [ ] V2.0: Web/远程控制