#!/usr/bin/env python3
"""打包后的可执行文件入口。

设计成一个入口同时服务两种用法：

* **双击运行**（没有命令行参数）→ 直接打开图形界面，这是普通用户期望的行为；
* **带参数运行**（``bitxk.exe grab --dry-run``）→ 走命令行。

之所以不用 ``--windowed`` 打包后只出 GUI：那样在 Windows 上会丢掉控制台，
命令行用户就没法用了。所以发布包里给两个可执行文件：

* ``bitxk-gui`` —— 无控制台，双击即开图形界面；
* ``bitxk``     —— 带控制台，供命令行 / 脚本 / 计划任务使用。
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import sys

#: 精简版（不含图形界面）里 tkinter 会被裁掉，这里做成可选导入
try:
    import tkinter
except ImportError:  # pragma: no cover - 取决于打包配置
    tkinter = None


def _running_as_gui() -> bool:
    """判断是否应当直接进入图形界面。"""
    if os.environ.get("BITXK_FORCE_GUI") == "1":
        return True
    if os.environ.get("BITXK_FORCE_CLI") == "1":
        return False
    # 没有任何参数 = 双击启动，按 GUI 处理
    return len(sys.argv) <= 1


def main() -> int:
    # PyInstaller 打包后如果代码里用到 multiprocessing，必须调这个，
    # 否则子进程会重复执行入口逻辑（Windows 上表现为窗口不断弹出）。
    # 本项目并不使用多进程，所以这里做成可选：即使打包时裁掉了
    # multiprocessing 模块，也不能因此启动失败。
    with contextlib.suppress(ImportError, AttributeError):
        multiprocessing.freeze_support()

    # 没有命令行参数 = 双击启动 → 直接进图形界面。
    #
    # 注意不能写成 cli_main(None)：argparse 里 None 的含义是
    # "请自己去读 sys.argv"，而这里恰恰是"没有任何参数"的场景，
    # 两者语义相反，会导致双击时打印帮助而不是打开窗口。
    if _running_as_gui():
        if tkinter is None:
            print(
                "这个可执行文件是精简的命令行版本，不含图形界面。\n"
                "请改用命令行，例如：\n"
                "  bitxk init          生成配置\n"
                "  bitxk browser-login 用浏览器登录\n"
                "  bitxk grab          开始抢课",
                file=sys.stderr,
            )
            return 1
        try:
            from bitxk.gui import run_gui
        except ImportError as exc:
            print(
                f"这个可执行文件没有内置图形界面支持（{exc}）。\n"
                "请改用同目录下的命令行版本，例如：bitxk grab",
                file=sys.stderr,
            )
            return 1

        from bitxk.exceptions import BitxkError

        try:
            return run_gui(None)
        except BitxkError as exc:
            # 没有显示环境（SSH / 无桌面）时给出人话提示
            print(f"{exc}", file=sys.stderr)
            return 1

    # 带参数 = 命令行用法
    from bitxk.cli import main as cli_main

    return int(cli_main())


if __name__ == "__main__":
    sys.exit(main())
