"""提醒：终端响铃 + 系统通知。

抢课成功的瞬间用户在干别的事，所以要能吵醒他。
所有方式都是「尽力而为」，失败不影响主流程。
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

__all__ = ["bell", "notify", "Notify"]


def bell(times: int = 3) -> None:
    """终端响铃。没有控制台时静默跳过（GUI 版就是这种情况）。"""
    # 注意不要直接写 sys.stdout：PyInstaller 的 windowed 打包下它是 None，
    # 而 GUI 版抢课成功时会走到这里。
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    try:
        for _ in range(max(1, times)):
            stream.write("\a")
        stream.flush()
    except Exception:  # pragma: no cover
        pass


def notify(title: str, message: str) -> bool:
    """发系统通知，返回是否成功。"""
    system = platform.system()

    try:
        if system == "Darwin":
            script = (
                f"display notification {_as_applescript(message)} "
                f'with title {_as_applescript(title)} sound name "Glass"'
            )
            subprocess.run(
                ["osascript", "-e", script],
                check=False,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True

        if system == "Linux":
            if shutil.which("notify-send"):
                subprocess.run(
                    ["notify-send", title, message],
                    check=False,
                    timeout=5,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            return False

        if system == "Windows":  # pragma: no cover - 非开发平台
            # 用 PowerShell 弹一个气泡通知
            ps = (
                "[reflection.assembly]::loadwithpartialname("
                "'System.Windows.Forms');"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                "$n.Visible=$true;"
                f"$n.ShowBalloonTip(10000,{_as_ps(title)},{_as_ps(message)},0)"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                check=False,
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("系统通知失败：%s", exc)

    return False


def _as_applescript(text: str) -> str:
    """转义成 AppleScript 字符串字面量。"""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _as_ps(text: str) -> str:
    """转义成 PowerShell 字符串字面量。"""
    return "'" + text.replace("'", "''") + "'"


class Notify:
    """按配置发送提醒。"""

    def __init__(self, *, sound: bool = True, desktop: bool = True) -> None:
        self.sound = sound
        self.desktop = desktop
        self._desktop_available = True

    def success(self, course: str, detail: str = "") -> None:
        title = "抢课成功"
        message = f"{course} {detail}".strip()
        if self.sound:
            bell(3)
        if self.desktop and self._desktop_available:
            self._desktop_available = notify(title, message)

    def alert(self, title: str, message: str) -> None:
        if self.sound:
            bell(2)
        if self.desktop and self._desktop_available:
            self._desktop_available = notify(title, message)
