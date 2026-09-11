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

import multiprocessing
import os
import sys


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
    multiprocessing.freeze_support()

    # 打包后 tkinter 的资源路径由 PyInstaller 处理，这里只兜住
    # "没有 tkinter 却想开 GUI" 的情况，给出人话提示。
    if _running_as_gui():
        try:
            import tkinter  # noqa: F401
        except ImportError:
            print(
                "这个可执行文件没有内置图形界面支持。\n"
                "请改用同目录下的 bitxk 命令行版本，例如：bitxk grab",
                file=sys.stderr,
            )
            return 1

    from bitxk.cli import main as cli_main

    return int(cli_main(None))


if __name__ == "__main__":
    sys.exit(main())
