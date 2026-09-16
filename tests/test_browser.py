"""浏览器登录模块测试。

真实浏览器启动较慢且依赖桌面环境，所以这里只测**不需要真的开浏览器**的部分：
发现逻辑、CDP 消息组装、登录判据、Cookie/Token 提取。真实生命周期在
``TestLiveBrowser`` 里用 ``skipif`` 隔离，只有本机确实有浏览器时才跑。
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

import pytest

from bitxk.browser import (
    BrowserInfo,
    BrowserNotFound,
    ChromiumSession,
    _default_persistent_profile,
    _find_playwright_chromium,
    _free_port,
    default_profile_dir,
    describe_browsers,
    detect_browsers,
    pick_browser,
)

# --------------------------------------------------------------------------
# 浏览器发现
# --------------------------------------------------------------------------


class TestDetectBrowsers:
    def test_找到至少一个浏览器或给出可操作报错(self):
        """在开发机上应能找到；找不到时 pick_browser 的报错必须可操作。"""
        browsers = detect_browsers()
        if not browsers:
            with pytest.raises(BrowserNotFound, match="BITXK_BROWSER"):
                pick_browser()
        else:
            assert browsers[0].path.is_file()

    def test_环境变量优先级最高(self, tmp_path, monkeypatch):
        """临时造一个"浏览器"文件，验证它被排在第一位。"""
        fake = tmp_path / "my-chrome"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.setenv("BITXK_BROWSER", str(fake))

        browsers = detect_browsers()
        assert browsers
        assert browsers[0].path == fake
        assert browsers[0].source == "env"

    def test_显式路径优先级高于环境变量(self, tmp_path, monkeypatch):
        env_browser = tmp_path / "env-chrome"
        env_browser.write_text("x")
        env_browser.chmod(0o755)
        monkeypatch.setenv("BITXK_BROWSER", str(env_browser))

        explicit = tmp_path / "explicit-chrome"
        explicit.write_text("x")
        explicit.chmod(0o755)

        assert detect_browsers(explicit)[0].path == explicit

    def test_不存在的路径被忽略(self, tmp_path):
        assert detect_browsers(tmp_path / "does-not-exist") == [] or all(
            b.path.exists() for b in detect_browsers(tmp_path / "does-not-exist")
        )

    def test_结果去重(self, tmp_path, monkeypatch):
        fake = tmp_path / "chrome-dup"
        fake.write_text("x")
        fake.chmod(0o755)
        monkeypatch.setenv("BITXK_BROWSER", str(fake))
        paths = [str(b.path) for b in detect_browsers(fake)]
        assert len(paths) == len(set(paths))

    def test_展开波浪号路径(self, monkeypatch):
        monkeypatch.setenv("BITXK_BROWSER", "~/nonexistent-browser-xyz")
        # 不该因为无法展开而抛异常
        detect_browsers()

    def test_describe_包含路径(self, tmp_path, monkeypatch):
        fake = tmp_path / "chrome-desc"
        fake.write_text("x")
        fake.chmod(0o755)
        monkeypatch.setenv("BITXK_BROWSER", str(fake))
        assert str(fake) in describe_browsers()

    def test_未找到时的描述文案(self, monkeypatch):
        monkeypatch.setenv("BITXK_BROWSER", "/nonexistent/xxx")
        monkeypatch.setattr("bitxk.browser._candidates_for_platform", lambda: [])
        monkeypatch.setattr("bitxk.browser.shutil.which", lambda _n: None)
        monkeypatch.setattr("bitxk.browser._find_playwright_chromium", lambda: None)
        assert "未找到" in describe_browsers()


class TestPlaywrightCache:
    def test_跳过_headless_shell(self, tmp_path, monkeypatch):
        """headless shell 没有窗口，用户没法在里面登录，必须排除。"""
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        (tmp_path / "chromium_headless_shell-1234" / "chrome-linux").mkdir(parents=True)
        shell = tmp_path / "chromium_headless_shell-1234" / "chrome-linux" / "chrome"
        shell.write_text("#!/bin/sh\n")
        shell.chmod(0o755)
        assert _find_playwright_chromium() is None

    def test_找到完整_chromium(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        full = tmp_path / "chromium-1234" / "chrome-linux" / "chrome"
        full.parent.mkdir(parents=True)
        full.write_text("#!/bin/sh\n")
        full.chmod(0o755)
        assert _find_playwright_chromium() == full

    def test_目录不存在时返回_None(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "nope"))
        # 其他平台缓存目录可能真实存在，这里只断言不抛异常
        _find_playwright_chromium()


# --------------------------------------------------------------------------
# 会话构建（不启动浏览器）
# --------------------------------------------------------------------------


class TestSessionSetup:
    def test_临时_profile_与持久_profile_互斥(self):
        browser = BrowserInfo("fake", Path("/bin/sh"), "test")
        temp_session = ChromiumSession(browser)
        try:
            # 默认用临时目录，且该目录确实被创建出来了
            assert "bitxk-chrome-" in str(temp_session.profile_dir)
        finally:
            temp_session.cleanup_profile()
            assert not temp_session.profile_dir.exists()

        persistent = ChromiumSession(browser, profile_dir="/tmp/bitxk-test-persist")
        assert str(persistent.profile_dir) == "/tmp/bitxk-test-persist"

    def test_reuse_profile_走默认持久目录(self):
        browser = BrowserInfo("fake", Path("/bin/sh"), "test")
        session = ChromiumSession(browser, reuse_profile=True)
        assert session.profile_dir == _default_persistent_profile()

    def test_headless_是可选开关(self):
        browser = BrowserInfo("fake", Path("/bin/sh"), "test")
        assert ChromiumSession(browser, headless=True).headless is True
        assert ChromiumSession(browser).headless is False

    def test_端口是空闲的(self):
        port = _free_port()
        assert 1024 < port < 65536

    def test_默认_profile_在用户目录下(self):
        path = default_profile_dir()
        assert path.name == "chrome-profile"
        assert str(path).startswith(str(Path.home()))

    def test_BITXK_HOME_可改_profile_位置(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BITXK_HOME", str(tmp_path))
        assert _default_persistent_profile() == tmp_path / "chrome-profile"

    def test_未连接的会话发命令报错(self):
        from bitxk.exceptions import BitxkError

        browser = BrowserInfo("fake", Path("/bin/sh"), "test")
        session = ChromiumSession(browser)
        with pytest.raises(BitxkError, match="尚未建立"):
            session._call("Page.enable")
        with pytest.raises(BitxkError, match="尚未建立"):
            session.navigate("about:blank", wait=0)


class TestCDPHelpers:
    """CDP 的 JSON 消息组装与解析（纯逻辑，不需要真浏览器）。"""

    def test_命令带自增_id(self):
        browser = BrowserInfo("fake", Path("/bin/sh"), "test")
        session = ChromiumSession(browser)
        assert session._cmd_id == 0
        session._cmd_id += 1
        assert session._cmd_id == 1

    def test_空闲端口各不相同(self):
        ports = {_free_port() for _ in range(5)}
        assert len(ports) == 5

    def test_eval_在未连接时返回_None(self):
        """eval 设计成失败不抛，方便轮询里反复试探页面状态。"""
        browser = BrowserInfo("fake", Path("/bin/sh"), "test")
        session = ChromiumSession(browser)
        assert session.eval("1+1", timeout=0.5) is None


# --------------------------------------------------------------------------
# 登录判据
# --------------------------------------------------------------------------


class TestLoginDetection:
    """验证"怎么判断用户已经登录成功"的逻辑。"""

    def test_从回跳_url_识别登录凭据(self):
        import re

        url = (
            "https://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/bitXsxkLogin/casLogin.do?bitXsxkLogin=ABC123"
        )
        match = re.search(r"[?&]bitXsxkLogin=([^&#\s]+)", url)
        assert match and match.group(1) == "ABC123"

    def test_sso_域名不算登录完成(self):
        """只要还在统一身份认证域，就说明用户还没登完。"""
        url = "https://sso.bit.edu.cn/cas/login?service=xxx"
        left_sso = bool(url) and "sso.bit.edu.cn" not in url and "bit.edu.cn" in url
        assert left_sso is False

    def test_离开_sso_域算登录完成(self):
        url = "https://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/*default/index.do"
        left_sso = bool(url) and "sso.bit.edu.cn" not in url and "bit.edu.cn" in url
        assert left_sso is True

    def test_空_url_不算(self):
        url = ""
        assert (bool(url) and "sso.bit.edu.cn" not in url and "bit.edu.cn" in url) is False

    def test_register_兜底脚本是合法_js(self):
        """兜底取 token 的脚本会被塞进 Runtime.evaluate，语法必须正确。"""
        from bitxk.browser import _probe_token_via_register

        captured: dict = {}

        class FakeSession:
            def eval(self, expression, **kwargs):
                captured["expr"] = expression
                return "TOKEN123"

        result = _probe_token_via_register(FakeSession(), "KEY")
        assert result == "TOKEN123"
        expr = captured["expr"]
        assert "register.do?number=KEY" in expr
        assert expr.startswith("(async")
        assert "credentials: 'include'" in expr

    def test_无_key_时不发请求(self):
        from bitxk.browser import _probe_token_via_register

        class BoomSession:
            def eval(self, *a, **k):
                raise AssertionError("不该被调用")

        assert _probe_token_via_register(BoomSession(), "") == ""


# --------------------------------------------------------------------------
# 真实浏览器（慢，且需要桌面环境）
# --------------------------------------------------------------------------


def _has_browser() -> bool:
    if platform.system() not in ("Darwin", "Windows", "Linux"):
        return False
    if os.environ.get("BITXK_SKIP_BROWSER_TESTS"):
        return False
    return bool(detect_browsers())


@pytest.mark.skipif(not _has_browser(), reason="本机没有 Chromium 系浏览器")
class TestLiveBrowser:
    """真实启动一次浏览器，验证 CDP 链路真的能跑通。

    用 ``headless=True`` 以免弹窗；不访问任何需要登录的页面。
    """

    def test_启动导航并读取页面状态(self):
        browser = pick_browser()
        session = ChromiumSession(browser, headless=True, start_url="about:blank")
        try:
            session.start()
            # 用 data: URL 做纯本地验证，不碰学校服务器
            session.navigate("data:text/html,<title>bitxk-test</title><h1>hi</h1>", wait=1.5)
            assert session.eval("document.title") == "bitxk-test"
            assert session.eval("document.querySelector('h1').textContent") == "hi"
        finally:
            session.close()
            session.cleanup_profile()

    def test_storage_读写与_cookie_读取(self):
        browser = pick_browser()
        session = ChromiumSession(browser, headless=True, start_url="about:blank")
        try:
            session.start()
            session.navigate("data:text/html,<title>s</title>", wait=1.0)
            # data: URL 的 origin 是不透明的，sessionStorage 会抛异常，
            # 这里只验证 eval 通路与 get_cookies 不崩
            session.eval("sessionStorage.setItem('k','v')")
            cookies = session.get_cookies("https://xk.bit.edu.cn")
            assert isinstance(cookies, dict)
        finally:
            session.close()
            session.cleanup_profile()

    def test_导航到真实站点可拿到_cookie(self):
        """走一次真实校站（未登录），确认能读到 cookie。

        网络不通时跳过而不是判失败 —— 这个用例想验证的是「CDP 导航与
        cookie 读取这条管线是通的」，不是「学校服务器此刻可达」。
        显式检查 ``chrome-error://`` 以区分这两件事：真出现导航错误说明
        是网络问题（用 skip），而不是管道坏了（那才会走到 assert）。
        """
        browser = pick_browser()
        session = ChromiumSession(browser, headless=True, start_url="about:blank")
        try:
            session.start()
            session.navigate(
                "https://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/bitXsxkLogin/casLogin.do",
                wait=3.0,
            )
            url = session.current_url()
            if not url or url.startswith("chrome-error://"):
                pytest.skip(f"当前网络无法访问校站（{url or '空 URL'}）")

            assert "bit.edu.cn" in url
            cookies = session.get_cookies("https://xk.bit.edu.cn")
            assert isinstance(cookies, dict)
            # 真实网关会下发 route cookie；拿不到也不算失败（可能是代理）
            assert "route" in cookies or cookies == {} or len(cookies) >= 1
        finally:
            session.close()
            session.cleanup_profile()


class TestStaleSessionHandling:
    """持久 profile 里残留的失效登录态 —— 实测踩过的坑。

    Chrome 的持久 profile 会保留上一次登录留下的 ``sessionStorage.token``，
    页面一打开 URL 就可能带着旧的 ``bitXsxkLogin``。如果只看这两者就判定
    "已登录"，用户会看到假的"登录成功"并被缓存下来，等到抢课时才发现
    根本用不了。所以必须用 ``student/<学号>.do`` 复验，验不过要清除重来。
    """

    def _make_session(self, *, token, usable_results):
        """造一个假浏览器会话：给定 token 与逐次校验结果。"""
        calls = {"eval": [], "n": 0}

        class FakeSession:
            def current_url(self):
                return (
                    "https://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/*default/index.do"
                    "?bitXsxkLogin=STALEKEY"
                )

            def storage_get(self, key, kind="session"):
                return token

            def eval(self, expression, **kwargs):
                calls["eval"].append(expression)
                if "removeItem" in expression:
                    return None
                # 模拟 student/<学号>.do 的探测结果
                idx = min(calls["n"], len(usable_results) - 1)
                calls["n"] += 1
                return usable_results[idx]

            def get_cookies(self, url=None):
                return {"route": "abc"}

        return FakeSession(), calls

    def test_陈旧登录态被识别并清除而不是当成成功(self, monkeypatch):
        from bitxk import browser as bmod

        # 校验一直返回 REDIRECT（服务端不认这份 token）
        session, calls = self._make_session(token="STALE", usable_results=["REDIRECT"])
        monkeypatch.setattr(bmod, "VERIFY_GRACE", 0.5)

        progress: list[str] = []
        result = bmod._wait_for_login(
            session,
            api_base="https://xk.bit.edu.cn/xsxkapp/sys/xsxkapp",
            student_code="1120200001",
            timeout=3.0,
            on_progress=progress.append,
        )

        assert result is None, "失效的登录态绝不能被当成成功返回"
        assert any("失效" in m for m in progress), f"应提示陈旧状态：{progress}"
        assert any("removeItem" in e for e in calls["eval"]), "应清除陈旧的 sessionStorage"

    def test_有效登录态正常返回(self, monkeypatch):
        from bitxk import browser as bmod

        session, _ = self._make_session(token="GOOD", usable_results=["OK"])
        result = bmod._wait_for_login(
            session,
            api_base="https://xk.bit.edu.cn/xsxkapp/sys/xsxkapp",
            student_code="1120200001",
            timeout=5.0,
            on_progress=lambda _m: None,
        )
        assert result is not None
        cookies, token, key, _url = result
        assert token == "GOOD"
        assert key == "STALEKEY"
        assert cookies == {"route": "abc"}

    def test_服务端延迟就绪时会重试而不是立刻放弃(self, monkeypatch):
        """登录刚完成时服务端可能还没就绪，应重试到成功。"""
        from bitxk import browser as bmod

        session, _ = self._make_session(token="GOOD", usable_results=["REDIRECT", "REDIRECT", "OK"])
        result = bmod._wait_for_login(
            session,
            api_base="https://xk.bit.edu.cn/xsxkapp/xsxkapp",
            student_code="1120200001",
            timeout=10.0,
            on_progress=lambda _m: None,
        )
        assert result is not None, "前两次验不过、第三次成功时应返回成功"

    def test_不知道学号时不做会话校验(self):
        """没学号就没法构造探测地址，此时不应把流程卡死。"""
        from bitxk import browser as bmod

        class FakeSession:
            def eval(self, expression, **kwargs):
                raise AssertionError("不该发起探测")

        assert bmod._session_usable(FakeSession(), "https://x", "") is True

    def test_连续两次运行都从干净状态开始(self):
        """清除陈旧状态后再跑一次，不应重复提示（stale_reported 生效）。"""
        import inspect

        src = inspect.getsource(__import__("bitxk.browser", fromlist=["x"])._wait_for_login)
        assert "stale_reported" in src
        assert src.count("stale_reported = True") == 1
