"""Chromium 浏览器自动化：发现、启动、以及通过 CDP 提取登录态。

为什么用浏览器而不是继续逆向 SSO
---------------------------------
直接模拟 SSO 登录需要复刻 AES 加密、执行串、风控字段……学校一改版就失效。
换成"让真人在真浏览器里登录一次，脚本只负责把登录态取出来"之后：

* 学校无论怎么改登录页，都不影响本工具；
* 短信验证码、2FA、密码管理器全都能正常工作；
* 不接触用户的密码，安全上更干净。

实现方式
--------
用 Chromium 自带的 **DevTools Protocol (CDP)**：给浏览器加
``--remote-debugging-port``，然后通过 WebSocket 发 CDP 命令。

刻意**不依赖 Playwright / Selenium**：

* Playwright 要额外下载 ~200MB 浏览器；
* 本机已有的 Chrome / Edge / Chromium 可以直接用；
* CDP 就是一层 WebSocket + JSON，用 ``websockets`` 库几十行就能驱动。

跨平台发现顺序（``detect_browsers``）
------------------------------------
1. 调用方显式指定的路径（``--browser`` / GUI 里选的）
2. 环境变量 ``BITXK_BROWSER``
3. 系统已安装的 Chrome / Edge / Chromium / Brave
4. Playwright 已下载的完整 Chromium（自动跳过 headless shell）
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exceptions import BitxkError

logger = logging.getLogger(__name__)

__all__ = [
    "BrowserNotFound",
    "BrowserInfo",
    "detect_browsers",
    "ChromiumSession",
    "LoginResult",
    "browser_login",
]

#: 选课系统里 Token 会落在 sessionStorage 的这个键上（前端就是这么存的）
_TOKEN_STORAGE_KEY = "token"
_STUDENT_STORAGE_KEY = "studentInfo"


class BrowserNotFound(BitxkError):
    """本机找不到可用的 Chromium 系浏览器。"""


# --------------------------------------------------------------------------
# 浏览器发现
# --------------------------------------------------------------------------


@dataclass
class BrowserInfo:
    """一个可用的 Chromium 系浏览器。"""

    name: str
    path: Path
    source: str  # env / system / playwright / config

    def __str__(self) -> str:
        return f"{self.name} ({self.path})"


#: 各平台上 Chromium 系浏览器的常见安装位置。
#: 只列真正的浏览器可执行文件，不含 helper / crashpad。
_MAC_CANDIDATES: list[tuple[str, str]] = [
    ("Google Chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ("Google Chrome (用户目录)", "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ("Microsoft Edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ("Chromium", "/Applications/Chromium.app/Contents/MacOS/Chromium"),
    ("Brave", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
    ("Vivaldi", "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi"),
]

_WINDOWS_CANDIDATES: list[tuple[str, str]] = [
    ("Google Chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ("Google Chrome (x86)", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ("Google Chrome (用户)", r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ("Microsoft Edge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ("Microsoft Edge (64)", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ("Chromium", r"%LOCALAPPDATA%\Chromium\Application\chrome.exe"),
]

_LINUX_CANDIDATES: list[tuple[str, str]] = [
    ("Google Chrome", "/usr/bin/google-chrome"),
    ("Google Chrome (stable)", "/usr/bin/google-chrome-stable"),
    ("Chromium", "/usr/bin/chromium"),
    ("Chromium (snap)", "/snap/bin/chromium"),
    ("Microsoft Edge", "/usr/bin/microsoft-edge"),
    ("Brave", "/usr/bin/brave-browser"),
]


def _candidates_for_platform() -> list[tuple[str, str]]:
    system = platform.system()
    if system == "Darwin":
        return _MAC_CANDIDATES
    if system == "Windows":
        return _WINDOWS_CANDIDATES
    return _LINUX_CANDIDATES


def _playwright_cache_roots() -> list[Path]:
    """Playwright 下载的浏览器缓存目录。"""
    if platform.system() == "Darwin":
        roots = [Path.home() / "Library/Caches/ms-playwright"]
    elif platform.system() == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        roots = [Path(local) / "ms-playwright"] if local else []
    else:
        roots = [Path.home() / ".cache/ms-playwright"]
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        roots.insert(0, Path(override))
    return [r for r in roots if r.is_dir()]


def _find_playwright_chromium() -> Path | None:
    """在 Playwright 缓存里找一个**带界面**的 Chromium。

    注意排除 ``chromium_headless_shell`` —— 那是无头专用壳，
    没有窗口，无法让用户在里面登录。
    """
    for root in _playwright_cache_roots():
        for entry in sorted(root.glob("chromium-*"), reverse=True):
            if "headless" in entry.name:
                continue
            for name in (
                "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
                "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
                "chrome-linux/chrome",
                "chrome-win/chrome.exe",
            ):
                candidate = entry / name
                if candidate.is_file():
                    return candidate
    return None


def detect_browsers(extra_path: str | Path | None = None) -> list[BrowserInfo]:
    """按优先级列出本机可用的 Chromium 系浏览器。

    Args:
        extra_path: 用户手工指定的浏览器路径，优先级最高。

    Returns:
        去重后的候选列表，第一个即最佳选择；空列表表示没找到。
    """
    found: list[BrowserInfo] = []
    seen: set[str] = set()

    def add(name: str, raw: str | Path, source: str) -> None:
        if not raw:
            return
        path = Path(os.path.expandvars(str(raw))).expanduser()
        try:
            if not path.is_file():
                return
        except OSError:
            return
        key = str(path.resolve())
        if key in seen:
            return
        seen.add(key)
        found.append(BrowserInfo(name=name, path=path, source=source))

    # 1) 调用方显式指定 —— 最明确的意图，优先级最高
    add("配置指定的浏览器", extra_path or "", "config")
    # 2) 环境变量
    add("环境变量指定的浏览器", os.environ.get("BITXK_BROWSER", ""), "env")
    # 3) 系统安装
    for name, raw in _candidates_for_platform():
        add(name, raw, "system")
    # 4) PATH 上的通用名
    for exe in ("google-chrome", "chromium", "chromium-browser", "msedge", "brave-browser"):
        located = shutil.which(exe)
        if located:
            add(exe, located, "system")
    # 5) Playwright 缓存
    pw = _find_playwright_chromium()
    if pw:
        add("Playwright Chromium", pw, "playwright")

    return found


def pick_browser(extra_path: str | Path | None = None) -> BrowserInfo:
    """选一个浏览器，找不到就抛 :class:`BrowserNotFound`。"""
    browsers = detect_browsers(extra_path)
    if not browsers:
        raise BrowserNotFound(
            "没有在本机找到 Chromium 系浏览器（Chrome / Edge / Chromium / Brave）。\n"
            "请任选一种方式解决：\n"
            "  1. 安装 Google Chrome；\n"
            "  2. 设置环境变量 BITXK_BROWSER 指向浏览器可执行文件；\n"
            "  3. 在工具设置里手工指定浏览器路径。"
        )
    return browsers[0]


# --------------------------------------------------------------------------
# CDP 客户端
# --------------------------------------------------------------------------


def _free_port() -> int:
    """要一个空闲端口给调试用。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_json(url: str, timeout: float = 2.0) -> Any:
    """取 CDP 的 HTTP 端点（/json/version、/json/list）。"""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ChromiumSession:
    """一个正在运行的 Chromium 实例，可发 CDP 命令。

    典型用法::

        with ChromiumSession() as session:
            session.navigate("https://xk.bit.edu.cn/...")
            ... 等用户手动登录 ...
            cookies = session.get_cookies("https://xk.bit.edu.cn")
    """

    def __init__(
        self,
        browser: BrowserInfo | None = None,
        *,
        port: int | None = None,
        profile_dir: str | Path | None = None,
        reuse_profile: bool = False,
        headless: bool = False,
        start_url: str = "about:blank",
        launch_timeout: float = 25.0,
    ) -> None:
        self.browser = browser or pick_browser()
        self.port = port or _free_port()
        self.headless = headless
        self.start_url = start_url
        self.launch_timeout = launch_timeout

        # 独立 profile 是刻意的：直接复用用户日常 Chrome 的 profile 会与
        # 已运行的 Chrome 抢目录（Chrome 用单例锁），而且会污染用户的浏览数据。
        self._temp_profile = profile_dir is None and not reuse_profile
        if profile_dir is not None:
            self.profile_dir = Path(profile_dir).expanduser()
        elif reuse_profile:
            self.profile_dir = _default_persistent_profile()
        else:
            self.profile_dir = Path(tempfile.mkdtemp(prefix="bitxk-chrome-"))

        self._process: subprocess.Popen | None = None
        self._owns_process = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._target_id: str | None = None
        self._cmd_id = 0

    # ------------------------------------------------------------ 生命周期

    def __enter__(self) -> ChromiumSession:
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def start(self) -> None:
        """启动浏览器并连上 CDP。

        如果这个端口上已经有一个带调试的浏览器（例如用户先前开过），
        就直接连上去，不再启动新的。
        """
        if self._is_port_live():
            logger.debug("端口 %d 上已有调试浏览器，直接复用", self.port)
            self._owns_process = False
        else:
            self._launch()
        self._connect()

    def _launch(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            str(self.browser.path),
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-extensions",
            "--disable-popup-blocking",
            # 远程调试默认只绑 127.0.0.1；显式声明以免被平台策略改掉
            "--remote-allow-origins=*",
        ]
        if self.headless:
            args.append("--headless=new")
        args.append(self.start_url)

        creationflags = 0
        if platform.system() == "Windows":  # pragma: no cover - 非开发平台
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        logger.debug("启动浏览器：%s", " ".join(args[:4]))
        self._process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self._owns_process = True
        self._wait_for_port()

    def _wait_for_port(self) -> None:
        deadline = time.time() + self.launch_timeout
        while time.time() < deadline:
            if self._is_port_live():
                return
            if self._process is not None and self._process.poll() is not None:
                raise BrowserNotFound(
                    f"浏览器启动后立刻退出（退出码 {self._process.returncode}）。"
                    "请确认该浏览器可以正常打开。"
                )
            time.sleep(0.25)
        raise BrowserNotFound(
            f"等待浏览器调试端口 {self.port} 超时。可能是安全软件拦截了本地调试端口。"
        )

    def _is_port_live(self) -> bool:
        try:
            _http_json(f"http://127.0.0.1:{self.port}/json/version", timeout=1.0)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def close(self) -> None:
        """关闭 WebSocket；仅在由本对象启动时结束浏览器进程。"""
        self._teardown_ws()
        if self._owns_process and self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=8)
            except (subprocess.TimeoutExpired, OSError):
                with contextlib.suppress(Exception):
                    self._process.kill()
        self._process = None

    def cleanup_profile(self) -> None:
        """删掉临时 profile 目录（只在本对象创建了临时目录时有效）。"""
        if not self._temp_profile:
            return
        with contextlib.suppress(Exception):
            shutil.rmtree(self.profile_dir, ignore_errors=True)

    # ------------------------------------------------------------ CDP 通信

    def _pick_target(self) -> dict:
        """挑一个 page target。优先已经是选课站的，否则第一个 page。"""
        targets = _http_json(f"http://127.0.0.1:{self.port}/json/list")
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            # 没有页面就开一个
            _http_json(f"http://127.0.0.1:{self.port}/json/new?about:blank")
            time.sleep(0.3)
            targets = _http_json(f"http://127.0.0.1:{self.port}/json/list")
            pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise BitxkError("浏览器里没有可用的页面标签")
        for page in pages:
            if "bit.edu.cn" in (page.get("url") or ""):
                return page
        return pages[0]

    def _connect(self) -> None:
        """建立到 page target 的 CDP WebSocket 连接。"""
        try:
            import websockets  # noqa: F401
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise BitxkError(
                "缺少 websockets 依赖，无法驱动浏览器。请先安装：pip install websockets"
            ) from exc

        target = self._pick_target()
        self._target_id = target.get("id")

        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._connect_async(target["webSocketDebuggerUrl"]))

    async def _connect_async(self, ws_url: str) -> None:
        import websockets

        # Chrome 从 111 起会校验 WebSocket 的 Origin 头；本机 CDP 直连时
        # 干脆不带 Origin，避免被拒（对应启动参数里的 --remote-allow-origins）。
        connect_kwargs: dict[str, Any] = {"max_size": 2**24, "open_timeout": 10}
        try:
            self._ws = await websockets.connect(ws_url, **connect_kwargs)
        except Exception:
            # 老版本 websockets 需要显式关掉 origin 检查
            self._ws = await websockets.connect(ws_url, suppress_origin=True, **connect_kwargs)
        await self._send("Page.enable")
        await self._send("Runtime.enable")
        await self._send("Network.enable")

    async def _send(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        if self._ws is None:
            raise BitxkError("CDP 连接尚未建立")
        self._cmd_id += 1
        cmd_id = self._cmd_id
        await self._ws.send(json.dumps({"id": cmd_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise BitxkError(f"CDP 命令 {method} 超时")
            raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            message = json.loads(raw)
            if message.get("id") == cmd_id:
                if "error" in message:
                    raise BitxkError(f"CDP {method} 失败：{message['error']}")
                return message.get("result", {})

    def _call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        """同步发一条 CDP 命令。"""
        if self._loop is None:
            raise BitxkError("CDP 连接尚未建立")
        return self._loop.run_until_complete(self._send(method, params, timeout))

    def _teardown_ws(self) -> None:
        if self._ws is not None and self._loop is not None:
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(self._ws.close())
        self._ws = None
        if self._loop is not None:
            with contextlib.suppress(Exception):
                self._loop.close()
        self._loop = None

    # ------------------------------------------------------------ 页面操作

    def navigate(self, url: str, *, wait: float = 1.0) -> None:
        """让当前标签跳转到指定地址。"""
        self._call("Page.navigate", {"url": url})
        if wait:
            time.sleep(wait)

    def eval(self, expression: str, *, timeout: float = 10.0) -> Any:
        """在页面里执行一段 JavaScript 并取回值。

        页面可能还在导航中，因此失败时短暂重试。
        """
        last_error: Exception | None = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = self._call(
                    "Runtime.evaluate",
                    {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                    timeout=5.0,
                )
                return result.get("result", {}).get("value")
            except BitxkError as exc:
                last_error = exc
                time.sleep(0.3)
        logger.debug("eval 失败：%s", last_error)
        return None

    def current_url(self) -> str:
        """当前页面地址（读 location.href，比 CDP 元数据更新更快）。"""
        return str(self.eval("location.href") or "")

    def storage_get(self, key: str, *, kind: str = "session") -> str | None:
        """读 sessionStorage / localStorage 里的一个键。"""
        store = "sessionStorage" if kind == "session" else "localStorage"
        value = self.eval(f"{store}.getItem({json.dumps(key)})")
        return str(value) if value not in (None, "") else None

    def get_cookies(self, url: str = "https://xk.bit.edu.cn") -> dict[str, str]:
        """取指定站点下的全部 Cookie（含 HttpOnly）。"""
        result = self._call("Network.getCookies", {"urls": [url]})
        cookies: dict[str, str] = {}
        for item in result.get("cookies", []) or []:
            name = item.get("name")
            if name:
                cookies[str(name)] = str(item.get("value", ""))
        return cookies

    def wait_for_url(self, predicate, *, timeout: float = 300.0, interval: float = 1.0) -> str:
        """轮询当前 URL 直到 ``predicate(url)`` 为真，返回该 URL。

        Args:
            predicate: 接受 URL 字符串、返回布尔值的函数。
            timeout: 最长等待秒数（登录可能需要输入密码 + 短信验证）。
        """
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            last = self.current_url()
            if last and predicate(last):
                return last
            time.sleep(interval)
        return last

    def bring_to_front(self) -> None:
        """把浏览器窗口提到最前，方便用户立刻看到并登录。"""
        with contextlib.suppress(Exception):
            self._call("Page.bringToFront", timeout=3.0)


def _default_persistent_profile() -> Path:
    """持久 profile 的位置（勾选"记住登录"时用）。

    放在用户目录下，这样下次打开浏览器还认得登录态，少登一次。
    """
    base = os.environ.get("BITXK_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".bitxk"
    return root / "chrome-profile"


# --------------------------------------------------------------------------
# 高层：浏览器登录取登录态
# --------------------------------------------------------------------------


@dataclass
class LoginResult:
    """一次浏览器登录的结果。"""

    session: Any  # bitxk.auth.Session
    cookies: dict[str, str] = field(default_factory=dict)
    token: str = ""
    login_key: str = ""
    source: str = ""
    student_code: str = ""
    student_name: str = ""


def browser_login(
    *,
    api_base: str,
    service_url: str,
    student_code: str = "",
    browser_path: str | Path | None = None,
    profile_dir: str | Path | None = None,
    remember: bool = True,
    timeout: float = 300.0,
    headless: bool = False,
    on_progress=None,
) -> LoginResult:
    """打开浏览器让用户登录，然后提取可用的登录态。

    流程::

        打开 Chromium → 跳到选课系统（自动跳统一身份认证）
        → 用户自己输账号密码（含 2FA/短信）
        → 脚本轮询直到发现登录成功
        → 提取 Cookie + Token
        → 关闭浏览器

    登录成功的判据（满足任一即可）：

    * 页面回跳地址里出现 ``bitXsxkLogin=<key>`` —— 最可靠；
    * ``student/<学号>.do`` 能返回数据；
    * ``sessionStorage`` 里出现了 token。

    Args:
        api_base: 选课系统接口基址。
        service_url: CAS 的 service 参数（即 ``bitXsxkLogin/casLogin.do``）。
        student_code: 学号。已知时用于构造学生信息接口地址。
        remember: 是否保留浏览器 profile（下次免登录）。``False`` 则用完即删。
        on_progress: 进度回调 ``(message: str) -> None``，用于 GUI 显示。

    Returns:
        :class:`LoginResult`，其中 ``session`` 可直接交给 :class:`bitxk.poller.Poller`。
    """
    from .auth import Session

    def progress(message: str) -> None:
        logger.debug(message)
        if on_progress is not None:
            with contextlib.suppress(Exception):
                on_progress(message)

    browser = pick_browser(browser_path)
    progress(f"使用浏览器：{browser.name}")

    entry = f"{api_base.rstrip('/')}/bitXsxkLogin/casLogin.do"

    # remember=True 用固定的持久 profile（下次还认得登录态）；
    # remember=False 交给 ChromiumSession 自己建临时目录，用完即删。
    effective_profile = profile_dir
    if effective_profile is None and remember:
        effective_profile = _default_persistent_profile()

    session = ChromiumSession(
        browser,
        profile_dir=effective_profile,
        headless=headless,
        start_url="about:blank",
    )
    ephemeral = effective_profile is None

    try:
        session.start()
        session.bring_to_front()
        progress("已在浏览器中打开登录页，请在窗口中完成登录…")
        session.navigate(entry, wait=2.0)

        result = _wait_for_login(
            session,
            api_base=api_base,
            student_code=student_code,
            timeout=timeout,
            on_progress=progress,
        )
        if result is None:
            raise BitxkError(
                f"等待登录超时（{int(timeout)} 秒）。请重新点击登录按钮，并在浏览器窗口里完成登录。"
            )

        cookies, token, key, url = result
        progress("已获取登录态，正在校验…")

        session_obj = Session(
            token=token,
            cookies=cookies,
            student_code=student_code,
            origin="browser",
        )

        # 学号/姓名很关键（批次接口是 student/<学号>.do），能探到就补上
        name, code = probe_student_identity(session_obj, student_code=student_code)
        if code:
            session_obj.student_code = code
        if name:
            session_obj.student_name = name

        return LoginResult(
            session=session_obj,
            cookies=cookies,
            token=token,
            login_key=key,
            source=url,
            student_code=session_obj.student_code,
            student_name=session_obj.student_name,
        )
    finally:
        session.close()
        if ephemeral:
            session.cleanup_profile()


def _wait_for_login(
    session: ChromiumSession,
    *,
    api_base: str,
    student_code: str,
    timeout: float,
    on_progress,
) -> tuple[dict[str, str], str, str, str] | None:
    """轮询浏览器，直到确认登录成功。

    Returns:
        ``(cookies, token, login_key, url)``；超时返回 ``None``。
    """
    import re

    deadline = time.time() + timeout
    announced = False
    last_url = ""

    while time.time() < deadline:
        url = session.current_url()
        if url and url != last_url:
            last_url = url
            logger.debug("当前页面：%s", url[:120])

        # 判据 1：回跳 URL 里带了选课系统的会话凭据
        match = re.search(r"[?&]bitXsxkLogin=([^&#\s]+)", url or "")
        key = match.group(1) if match else ""

        # 只要离开了统一身份认证域，就说明登录动作已经完成
        left_sso = bool(url) and "sso.bit.edu.cn" not in url and "bit.edu.cn" in url
        if left_sso and not announced:
            announced = True
            on_progress("检测到已登录，正在提取登录态…")

        # 判据 2：sessionStorage 里已有 token（前端登录成功后一定会写）
        token = session.storage_get(_TOKEN_STORAGE_KEY)

        # 判据 3：学生信息接口能通
        if not token and left_sso:
            token = _probe_token_via_register(session, key)

        if token:
            cookies = session.get_cookies("https://xk.bit.edu.cn")
            if not cookies:
                cookies = session.get_cookies()
            return cookies, token, key, url

        time.sleep(1.5)

    return None


def probe_student_identity(
    session, *, student_code: str = "", api_base: str = ""
) -> tuple[str, str]:
    """校验登录态并取回 ``(姓名, 学号)``。

    用 ``student/<学号>.do`` 验一次，能同时确认 token 真的可用、
    并拿到姓名。拿不到就返回空串，不影响调用方继续。

    Args:
        session: :class:`bitxk.auth.Session`。
        student_code: 已知学号；为空时用 ``session.student_code``。
        api_base: 选课系统接口基址，默认用 :data:`bitxk.auth.API_BASE`。
    """
    from .auth import API_BASE
    from .client import XkClient
    from .http import HttpClient

    code = student_code or getattr(session, "student_code", "") or ""
    if not code:
        return "", ""

    http = HttpClient(min_interval=0, timeout=12, max_retries=0)
    try:
        http.cookies = dict(getattr(session, "cookies", {}) or {})
        http.set_token(getattr(session, "token", "") or None)
        client = XkClient(http, api_base=api_base or API_BASE)
        info = client.student_info(code)
        name = str(info.get("name") or info.get("xm") or "")
        number = str(info.get("code") or info.get("number") or code)
        return name, number
    except Exception as exc:  # 校验失败不该让登录流程整体失败
        logger.debug("校验学生身份失败：%s", exc)
        return "", ""
    finally:
        http.close()


def _probe_token_via_register(session: ChromiumSession, login_key: str) -> str:
    """兜底取 token：直接问选课系统的 register 接口。

    前端登录成功后会把 token 写进 sessionStorage，正常路径读它就行。
    但如果页面还没走完前端逻辑（或者用户手动关掉了标签），
    可以在浏览器里 fetch 一次 ``register.do``，用同一个会话换 token。
    """
    if not login_key:
        return ""
    script = (
        "(async () => {"
        "  try {"
        f"    const r = await fetch('/xsxkapp/sys/xsxkapp/student/register.do?number={login_key}',"
        "      {credentials: 'include'});"
        "    const j = await r.json();"
        "    return (j && j.data && j.data.token) ? j.data.token : '';"
        "  } catch (e) { return ''; }"
        "})()"
    )
    value = session.eval(script, timeout=15.0)
    return str(value or "")


def describe_browsers(extra_path: str | Path | None = None) -> str:
    """给 GUI / CLI 用的人类可读浏览器清单。"""
    browsers = detect_browsers(extra_path)
    if not browsers:
        return "未找到 Chromium 系浏览器"
    lines = []
    for index, item in enumerate(browsers):
        mark = "（默认）" if index == 0 else ""
        lines.append(f"{index + 1}. {item.name}{mark}\n   {item.path}")
    return "\n".join(lines)


def default_profile_dir() -> Path:
    """默认持久 profile 目录（供 GUI 展示）。"""
    return _default_persistent_profile()


def is_frozen() -> bool:
    """是否运行在打包后的可执行文件里（PyInstaller 等）。"""
    return bool(getattr(sys, "frozen", False))
