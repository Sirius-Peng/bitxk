"""无控制台环境（PyInstaller 的 windowed 打包）下的健壮性。

真实的线上崩溃：Windows 用户双击 ``bitxk-gui.exe``，在登录态失效时看到

    'NoneType' object has no attribute 'isatty'

原因是 GUI 版是**无控制台**打包的，此时 ``sys.stdout`` / ``sys.stderr`` 是
``None``，而 ``bitxk.gui`` 里多处 ``from .cli import _build_http`` 会触发
``cli`` 模块级求值的 ``Style.enabled = _supports_color()``，它直接调用了
``sys.stdout.isatty()``。

这类 bug 有两个特别之处，测试必须还原才抓得住：

1. **只在 windowed 打包后才出现**。从终端启动 exe 时子进程会继承控制台句柄，
   stdout 不为 None，所以开发机上怎么点都是好的。
2. **会被 ``NO_COLOR`` 掩盖**。``_supports_color()`` 第一行就检查 ``NO_COLOR``，
   只要该变量存在就直接返回 False，根本走不到 ``isatty()``。开发者的 shell
   里往往恰好设了它（本项目作者的 macOS 终端就是 ``NO_COLOR=1``），
   于是本地全绿、用户全崩。

所以这里用子进程 + 显式清掉 ``NO_COLOR`` 来还原用户环境。子进程是必须的：
``Style.enabled`` 在导入时求值一次，同进程内改 ``sys.stdout`` 后再导入
拿不到那个求值时刻。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_without_streams(body: str) -> subprocess.CompletedProcess[str]:
    """在 stdout/stderr 皆为 None 的子进程里跑一段代码（模拟 windowed 打包）。

    结果通过临时文件回传 —— 子进程里已经没有可用的流了。
    """
    script = textwrap.dedent(
        f"""
        import sys, json, os
        result_path = os.environ["BITXK_TEST_RESULT"]
        sys.path.insert(0, {str(PROJECT_ROOT)!r})

        # PyInstaller 无控制台打包时就是这个状态
        sys.stdout = None
        sys.stderr = None

        outcome = {{}}
        try:
            {textwrap.indent(textwrap.dedent(body).strip(), " " * 12).lstrip()}
            outcome["error"] = None
        except BaseException as exc:
            outcome["error"] = f"{{type(exc).__name__}}: {{exc}}"

        with open(result_path, "w", encoding="utf-8") as fh:
            json.dump(outcome, fh)
        """
    )

    import json
    import os
    import tempfile

    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as tmp:
        result_path = tmp.name
    try:
        env = dict(os.environ)
        # 关键：NO_COLOR 会让 _supports_color() 提前返回，从而掩盖真正的崩溃
        env.pop("NO_COLOR", None)
        env["BITXK_TEST_RESULT"] = result_path
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
    finally:
        Path(result_path).unlink(missing_ok=True)

    proc.outcome = payload  # type: ignore[attr-defined]
    return proc


class TestWindowedPackaging:
    """模拟「双击无控制台的 exe」这一真实场景。"""

    def test_导入_cli_模块不应崩溃(self):
        """登录态失效时会走到这里（gui._verify_worker → from .cli import _build_http）。"""
        proc = _run_without_streams("import bitxk.cli")
        assert proc.outcome["error"] is None, (
            f"无控制台时导入 bitxk.cli 崩溃：{proc.outcome['error']}\n{proc.stderr}"
        )

    def test_导入_gui_模块不应崩溃(self):
        proc = _run_without_streams("import bitxk.gui")
        assert proc.outcome["error"] is None, (
            f"无控制台时导入 bitxk.gui 崩溃：{proc.outcome['error']}\n{proc.stderr}"
        )

    def test_颜色探测在无流时返回_false(self):
        proc = _run_without_streams(
            "from bitxk.cli import _supports_color; assert _supports_color() is False"
        )
        assert proc.outcome["error"] is None, proc.outcome["error"]

    def test_终端响铃在无流时不抛异常(self):
        """抢课成功会响铃，notify.bell() 不能用未受保护的 sys.stdout。"""
        proc = _run_without_streams("from bitxk.notify import bell; bell()")
        assert proc.outcome["error"] is None, proc.outcome["error"]

    def test_输出辅助函数在无流时静默丢弃(self):
        """cli 里的打印走 _echo/_safe，无控制台时必须安静地什么都不做。"""
        proc = _run_without_streams(
            """
            from bitxk.cli import _echo, _safe
            _echo("这条消息没有控制台可去")
            _echo("这条也应当被丢弃", err=True)
            _safe("带符号的消息: ✓ ✗ →")
            """
        )
        assert proc.outcome["error"] is None, proc.outcome["error"]

    def test_控制台准备在无流时不抛异常(self):
        proc = _run_without_streams("from bitxk.cli import _prepare_console; _prepare_console()")
        assert proc.outcome["error"] is None, proc.outcome["error"]


class TestNoColorStillHonoured:
    """修 bug 不能把 NO_COLOR 的语义弄坏。"""

    def test_NO_COLOR_时不着色(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        import importlib

        import bitxk.cli as cli

        importlib.reload(cli)
        try:
            assert cli._supports_color() is False
        finally:
            monkeypatch.delenv("NO_COLOR", raising=False)
            importlib.reload(cli)


@pytest.mark.parametrize("module", ["bitxk.cli", "bitxk.gui", "bitxk.notify", "bitxk.poller"])
def test_模块源码里没有裸露的流访问(module):
    """守住回归：源码里不允许再出现 ``sys.stdout.<attr>`` 这种直接调用。

    只允许经由 ``_stdout()`` / ``_stderr()`` 这类已经判空的辅助函数访问。
    """
    import importlib
    import re

    mod = importlib.import_module(module)
    source = Path(mod.__file__).read_text(encoding="utf-8")

    offenders = [
        (no, line.strip())
        for no, line in enumerate(source.splitlines(), 1)
        if re.search(r"sys\.(stdout|stderr)\s*\.", line) and "getattr" not in line
    ]
    assert not offenders, f"{module} 里存在未判空的流访问：{offenders}"
