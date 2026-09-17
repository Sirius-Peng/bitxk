"""命令行入口。

子命令
------
* ``init``    生成配置文件模板
* ``login``   验证账号密码能否登录成功，并缓存登录态
* ``list``    列出当前可选批次与指定课程的余量（看一眼就走，不轮询）
* ``grab``    正式轮询 + 自动抢课
* ``check``   不带账号的连通性与页面结构自检（排查环境问题用）
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time
from getpass import getpass as _getpass
from pathlib import Path
from typing import Any

from . import __version__
from .auth import API_BASE, CAS_LOGIN_URL, BitAuth, Session, SessionCheck, check_session
from .client import XkClient
from .config import DEFAULT_CONFIG_NAME, SAMPLE_CONFIG, Config, load_config
from .exceptions import (
    BitxkError,
    CaptchaRequired,
    ConfigError,
    LoginError,
    NotInBatchError,
    TokenExpired,
)
from .http import HttpClient
from .models import CourseStatus
from .notify import Notify
from .poller import Poller

logger = logging.getLogger("bitxk")

__all__ = ["main"]

# ANSI 颜色（终端不支持时自动降级）
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _stdout() -> Any:
    """取 ``sys.stdout``，无控制台时返回 ``None``。

    PyInstaller 用 ``console=False`` 打包出来的是**没有控制台**的程序，此时
    ``sys.stdout`` / ``sys.stderr`` 就是 ``None``。GUI 版正是这么打的，而
    ``bitxk.gui`` 里有多处 ``from .cli import _build_http`` —— 那会让本模块被
    导入，于是模块级求值的 ``Style.enabled`` 就会去碰 ``sys.stdout``。

    只要有一处忘了判空，用户在「登录态失效」时就会撞见
    ``'NoneType' object has no attribute 'isatty'``。所以所有流的访问都必须
    经过这两个函数，不要再直接写 ``sys.stdout``。
    """
    return getattr(sys, "stdout", None)


def _stderr() -> Any:
    """取 ``sys.stderr``，无控制台时返回 ``None``。理由见 :func:`_stdout`。"""
    return getattr(sys, "stderr", None)


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    stream = _stdout()
    if stream is None:
        return False
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (ValueError, OSError):
        # 流已经关了（打包后偶发），当作不支持
        return False


class Style:
    """极简着色器：非 TTY 时自动输出纯文本。"""

    enabled = _supports_color()

    @classmethod
    def _wrap(cls, text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if cls.enabled else text

    @classmethod
    def green(cls, text: str) -> str:
        return cls._wrap(text, _GREEN)

    @classmethod
    def red(cls, text: str) -> str:
        return cls._wrap(text, _RED)

    @classmethod
    def yellow(cls, text: str) -> str:
        return cls._wrap(text, _YELLOW)

    @classmethod
    def cyan(cls, text: str) -> str:
        return cls._wrap(text, _CYAN)

    @classmethod
    def dim(cls, text: str) -> str:
        return cls._wrap(text, _DIM)

    @classmethod
    def bold(cls, text: str) -> str:
        return cls._wrap(text, _BOLD)


#: 各平台的输出编码与可用符号。
#:
#: Windows 中文版的默认控制台编码是 **GBK（cp936）**，输出 ``✓`` 会直接抛
#: ``UnicodeEncodeError`` 让程序崩溃 —— 这是打包成 exe 后必现的问题。
#: 所以这里先尝试把控制台切到 UTF-8；切不动就退回纯 ASCII 符号。
def _prepare_console() -> tuple[str, str, str]:
    """返回 ``(ok记号, warn记号, err记号)``，并尽量让控制台能吃下它们。"""
    # 1) Windows：先把控制台代码页切到 UTF-8
    if sys.platform == "win32":  # pragma: no cover - 平台相关
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)

    # 2) 再让 Python 的 stdout 用 UTF-8（打包后 PYTHONIOENCODING 未必生效）
    for stream in (_stdout(), _stderr()):
        if stream is None:
            continue
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")

    # 3) 实际试一下能不能编码，不能就用 ASCII 兜底
    stream = _stdout()
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "✓✗!→".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return "[OK]", "[!]", "[x]"
    return "✓", "!", "✗"


_OK_MARK, _WARN_MARK, _ERR_MARK = _prepare_console()


#: 控制台吃不下 Unicode 时的 ASCII 替代表。
#: 只覆盖"会真正打印出来"的符号 —— 注释里的制表符、破折号不影响运行。
_ASCII_FALLBACK = {
    "✓": "[OK]",
    "✗": "[x]",
    "★": "*",
    "·": "-",
    "←": "<-",
    "→": "->",
    "…": "...",
    "—": "-",
    "─": "-",
    "×": "x",
    "≥": ">=",
    "🎉": "[OK]",
}


class _AsciiSafeStream:
    """包住 stdout/stderr，保证任何输出都不会因为编码问题崩掉。

    比在每个 print 上加 try 更彻底：连第三方库的输出、异常回溯
    都被同一层兜住。
    """

    def __init__(self, stream) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        try:
            return self._stream.write(text)
        except UnicodeEncodeError:
            return self._stream.write(_ascii_fallback(str(text)))

    def __getattr__(self, name):  # 其余属性透传给真实流
        return getattr(self._stream, name)


def _ascii_fallback(text: str) -> str:
    """把控制台编不出的符号替换成 ASCII 等价物。"""
    out = text
    for fancy, plain in _ASCII_FALLBACK.items():
        out = out.replace(fancy, plain)
    encoding = getattr(_stdout(), "encoding", None) or "utf-8"
    return out.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _install_stream_guard() -> None:
    """在 Windows 等 GBK 控制台上给输出流加保护。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or isinstance(stream, _AsciiSafeStream):
            continue
        encoding = getattr(stream, "encoding", None) or "utf-8"
        try:
            "✓→★".encode(encoding)
        except (UnicodeEncodeError, LookupError):
            setattr(sys, name, _AsciiSafeStream(stream))


def _safe(text: object) -> str:
    """把一段要打印的文本转成当前控制台能安全输出的形式。

    Windows 中文版控制台是 GBK，遇到 ``✓`` ``→`` 这类字符会直接抛
    ``UnicodeEncodeError`` 把程序打崩。这里在**唯一出口**统一兜住，
    比在每个 print 上加 try 更可靠。
    """
    s = str(text)
    encoding = getattr(_stdout(), "encoding", None) or "utf-8"
    try:
        s.encode(encoding)
        return s
    except (UnicodeEncodeError, LookupError):
        pass
    for fancy, plain in _ASCII_FALLBACK.items():
        s = s.replace(fancy, plain)
    # 仍有编不出来的字符（罕见），直接丢弃而不是崩掉
    return s.encode(encoding, errors="replace").decode(encoding, errors="replace")


# 定义齐了，现在给输出流装上保护
_install_stream_guard()


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def _echo(text: str, *, err: bool = False) -> None:
    """往控制台写一行；没有控制台就安静地丢掉。

    GUI 版是无控制台打包的（``sys.stdout is None``），但它会经
    ``from .cli import _build_http`` 用到本模块里的函数。只要这些函数顺手
    打印点什么，用户就会看到莫名的崩溃。所以统一从这里出口。

    注意这里用的是**查询每个调用点**而不是模块导入时缓存：测试和某些打包
    场景会在导入之后替换 ``sys.stdout``。
    """
    stream = _stderr() if err else _stdout()
    if stream is None:
        return
    with contextlib.suppress(Exception):
        print(_safe(text), file=stream, flush=True)


def log(message: str) -> None:
    _echo(f"{Style.dim(_stamp())} {message}")


def log_ok(message: str) -> None:
    log(f"{Style.green(_OK_MARK)} {message}")


def log_warn(message: str) -> None:
    log(f"{Style.yellow(_WARN_MARK)} {message}")


def log_err(message: str) -> None:
    log(f"{Style.red(_ERR_MARK)} {message}")


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------


def _add_common_options(parser: argparse.ArgumentParser, *, prefixed: bool = False) -> None:
    """给子命令也挂上常用选项。

    argparse 默认只认「子命令之前」的全局参数，``bitxk grab --token x`` 会被判为
    未知参数。而这些参数在实际使用中几乎总是写在子命令后面，
    所以在每个子命令上再注册一份。

    Args:
        prefixed: 子命令版本用 ``_`` 前缀的 dest，避免与主解析器的同名属性
            互相覆盖，解析后由 :func:`_merge_common` 合并。
    """
    dest = (lambda name: f"_{name}") if prefixed else (lambda name: name)
    parser.add_argument(
        "--cookie",
        dest=dest("cookie"),
        default=None,
        help="手动导入浏览器 Cookie（自动登录失效时的兜底）",
    )
    parser.add_argument(
        "--token",
        dest=dest("token"),
        default=None,
        help="手动导入选课系统 Token（或带 bitXsxkLogin= 的回跳 URL）",
    )
    parser.add_argument(
        "--encrypt-mode",
        dest=dest("encrypt_mode"),
        choices=("ecb", "cbc"),
        default=None,
        help="SSO 密码加密模式，默认 ecb；登录报错时可试 cbc",
    )
    parser.add_argument(
        "--dry-run",
        dest=dest("dry_run"),
        action="store_true",
        help="只查询余量，绝不提交选课（安全试跑）",
    )
    parser.add_argument(
        "--interval",
        dest=dest("interval"),
        type=float,
        default=None,
        help="轮询间隔秒数（覆盖配置）",
    )
    parser.add_argument(
        "--duration",
        dest=dest("duration"),
        type=float,
        default=None,
        help="最长运行秒数，0 表示不限（覆盖配置）",
    )
    parser.add_argument(
        "--student-code",
        dest=dest("student_code"),
        default=None,
        help="学号。手动导入登录态（--token）时必填，因为批次接口形如 student/<学号>.do",
    )
    parser.add_argument(
        "--browser",
        dest=dest("browser"),
        nargs="?",
        const="",
        default=None,
        metavar="路径",
        help="用 Chromium 浏览器登录来获取登录态（推荐，不受 SSO 改版影响）；"
        "可选用路径参数指定浏览器可执行文件",
    )
    parser.add_argument(
        "--browser-timeout",
        dest=dest("browser_timeout"),
        type=float,
        default=None,
        help="等待浏览器登录的超时秒数（默认 300）",
    )
    parser.add_argument(
        "--list-browsers",
        dest=dest("list_browsers"),
        action="store_true",
        help="列出本机检测到的 Chromium 系浏览器后退出",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bitxk",
        description="北京理工大学（wisedu 选课系统）课程余量轮询与自动选课工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  bitxk init                     生成配置模板\n"
            "  bitxk check                    自检网络与页面结构\n"
            "  bitxk login                    测试登录\n"
            "  bitxk list                     看当前余量\n"
            "  bitxk browser-login            用浏览器登录（推荐，不受 SSO 改版影响）\n"
            "  bitxk grab                     开始轮询抢课\n"
            "  bitxk gui                      打开图形界面\n"
            "  bitxk grab --browser           抢课前先用浏览器登录\n"
        ),
    )
    parser.add_argument("-V", "--version", action="version", version=f"bitxk {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help=f"配置文件路径（默认 ./{DEFAULT_CONFIG_NAME}）",
    )
    parser.add_argument("-u", "--username", default=None, help="学号（覆盖配置文件）")
    parser.add_argument(
        "-p",
        "--password",
        default=None,
        help="密码（覆盖配置文件；建议改用环境变量 BITXK_PASSWORD，避免留在 shell 历史里）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出关键事件")
    _add_common_options(parser)

    sub = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("init", "生成配置文件模板"),
        ("check", "自检：网络连通性与登录页结构"),
        ("login", "验证登录并缓存会话"),
        ("list", "列出当前批次与课程余量"),
    ):
        _add_common_options(sub.add_parser(name, help=help_text), prefixed=True)

    sub.add_parser("gui", help="打开图形界面（推荐给不熟悉命令行的同学）")

    browser = sub.add_parser(
        "browser-login",
        help="用 Chromium 浏览器登录一次并缓存登录态（推荐，不受 SSO 改版影响）",
    )
    _add_common_options(browser, prefixed=True)

    grab = sub.add_parser("grab", help="轮询并自动选课")
    _add_common_options(grab, prefixed=True)
    grab.add_argument("--once", action="store_true", help="只查一轮余量后退出（等同于 list）")
    return parser


def _merge_common(args) -> None:
    """把子命令上解析到的通用选项回填到主命名空间（子命令优先）。"""
    for name in (
        "cookie",
        "token",
        "encrypt_mode",
        "dry_run",
        "interval",
        "duration",
        "student_code",
        "browser",
        "browser_timeout",
        "list_browsers",
    ):
        value = getattr(args, f"_{name}", None)
        if value not in (None, False):
            setattr(args, name, value)


def parse_args(argv: list[str] | None = None):
    """解析参数并合并全局/子命令两处的通用选项。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    _merge_common(args)
    return args


# --------------------------------------------------------------------------
# 通用装配
# --------------------------------------------------------------------------


def _setup_logging(args) -> None:
    level = logging.WARNING
    if args.verbose:
        level = logging.DEBUG
    elif args.quiet:
        level = logging.ERROR
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # 第三方库的噪音压掉
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _safe_getpass(prompt: str) -> str:
    """安全地读密码。

    ``getpass`` 在非终端环境（管道、重定向、部分 CI）会直接抛 OSError / EOFError，
    而不是优雅降级。这里兜住，避免"只是重定向了输出"就让整个程序崩掉。
    """
    try:
        return _getpass(prompt)
    except (OSError, EOFError, AttributeError):
        logger.debug("getpass 不可用，改用普通读取")
        try:
            return input(prompt)
        except EOFError:
            return ""


def _resolve_credentials(cfg: Config, args) -> None:
    """按 命令行 > 环境变量 > 配置文件 > 交互输入 的优先级确定账号密码。"""
    # 浏览器登录 / 手动导入都不需要密码，绝不能在这里拦一道
    if _use_browser_login(args) or getattr(args, "token", None) or getattr(args, "cookie", None):
        if args.username:
            cfg.username = args.username
        return

    if args.username:
        cfg.username = args.username
    if args.password:
        cfg.password = args.password

    if cfg.username and not cfg.password:
        cfg.password = _safe_getpass(f"请输入 {cfg.username} 的密码：")
    elif not cfg.username:
        try:
            cfg.username = input("请输入学号：").strip()
        except EOFError:
            cfg.username = ""
        if cfg.username and not cfg.password:
            cfg.password = _safe_getpass("请输入密码：")


def _client(cfg: Config, http: HttpClient) -> XkClient:
    """按配置构造业务客户端（把用户填的地址归一化成 HTTPS）。"""
    from .auth import normalize_base_url

    return XkClient(http, api_base=normalize_base_url(cfg.api_base))


def _build_http(cfg: Config) -> HttpClient:
    return HttpClient(
        min_interval=cfg.poll.min_request_interval,
        timeout=cfg.http.timeout,
        max_retries=cfg.http.max_retries,
        verify=cfg.http.verify_ssl,
        proxy=cfg.http.proxy or None,
    )


def _build_auth(cfg: Config, http: HttpClient, args) -> BitAuth:
    return BitAuth(http, encrypt_mode=args.encrypt_mode or cfg.encrypt_mode)


def browser_login_session(cfg: Config, args, *, on_progress=None) -> Session:
    """用 Chromium 登录取登录态。

    走这条路完全不需要账号密码：让用户在自己熟悉的浏览器里登录一次，
    脚本只负责把 Cookie 与 Token 取出来。学校改版 SSO 也不受影响。

    Args:
        cfg: 配置（取学号、超时等）。
        args: 命令行参数（``--browser`` 可指定浏览器路径）。
        on_progress: 进度回调，供 GUI 复用。
    """
    from .auth import API_BASE, CAS_SERVICE_URL
    from .browser import browser_login

    browser_path = getattr(args, "browser", None) or None
    timeout = getattr(args, "browser_timeout", None) or 300.0
    progress = on_progress or (lambda msg: log(msg))

    if browser_path:
        log(f"使用指定的浏览器：{browser_path}")
    else:
        log("正在检测本机浏览器…")

    result = browser_login(
        api_base=API_BASE,
        service_url=CAS_SERVICE_URL,
        student_code=cfg.username,
        browser_path=browser_path,
        remember=True,
        timeout=float(timeout),
        on_progress=progress,
    )
    who = result.student_name or "（未知）"
    code = result.session.student_code or "（未取到学号）"
    log_ok(f"已从浏览器取得登录态：{who}（{code}）")
    log("  学号与姓名是自动从页面里读出来的，无需再手工指定 --student-code")

    if not result.session.student_code:
        raise ConfigError(
            "取到了登录态但没能读到学号。\n"
            "  请在浏览器窗口里等页面完全加载（能看到「开始选课」按钮）后重试；\n"
            "  或用 --student-code 手动指定。"
        )
    return result.session


def _manual_session(args, cfg: Config) -> Session | None:
    """如果用户给了 --token/--cookie，就构造手动会话。"""
    if not (args.token or args.cookie):
        return None
    session = BitAuth.from_manual(
        token=args.token or "",
        cookie=args.cookie or "",
        # 手动导入时学号可能只出现在命令行里（config 的 account 段可以是空的）
        student_code=getattr(args, "student_code", None) or cfg.username,
    )
    if not session.token:
        raise ConfigError(
            "手动导入模式需要 --token（选课系统 Token 或带 bitXsxkLogin= 的回跳 URL）。"
            "只给 --cookie 无法定位登录态。"
        )
    return session


def _has_usable_session(cfg: Config, args) -> bool:
    """是否已有可用的登录态（缓存会话 / 手动导入 / 浏览器登录）。

    有的话就不该再要求 config.toml 里填学号 —— 学号能从会话里拿到。
    """
    if _use_browser_login(args) or getattr(args, "token", None) or getattr(args, "cookie", None):
        return True
    cached = Session.load(cfg.base_dir / cfg.session_file)
    return bool(cached and cached.token and cached.student_code)


def _use_browser_login(args) -> bool:
    """是否走浏览器登录取登录态。"""
    return getattr(args, "browser", None) is not None


def _has_manual_session(args) -> bool:
    """是否走手动导入登录态的路径（不需要账号密码）。"""
    return bool(
        getattr(args, "token", None) or getattr(args, "cookie", None) or _use_browser_login(args)
    )


def _connect(cfg: Config, args, *, need_login: bool = True) -> tuple[HttpClient, XkClient, Session]:
    """装配 HttpClient / XkClient / Session。"""
    # 以下三种情况都不需要配置里的账号密码，因此不校验 account 段：
    #   1. 浏览器登录（--browser / browser-login）
    #   2. 手动导入（--token / --cookie）
    #   3. 本地已缓存了可用会话（学号能从会话里拿到）
    manual = _manual_session(args, cfg)
    cached_session = Session.load(cfg.base_dir / cfg.session_file)
    cfg.validate(require_account=not _has_usable_session(cfg, args))
    http = _build_http(cfg)

    if _use_browser_login(args):
        session = browser_login_session(cfg, args)
        session.save(cfg.base_dir / cfg.session_file)
        http.cookies = session.cookies
        http.set_token(session.token)
        http.student_code = session.student_code
        return http, _client(cfg, http), session

    if manual is not None:
        http.cookies = manual.cookies
        http.set_token(manual.token)
        if manual.student_code:
            http.student_code = manual.student_code
        return http, _client(cfg, http), manual

    # 复用本地缓存的登录态。
    #
    # 注意两个坑（都实测踩过）：
    #   1. 不能因为"用户传了 --username 或配置里有学号"就跳过复用 ——
    #      浏览器登录后就该直接复用缓存，跟有没有学号无关。旧实现要求
    #      not args.username，而学号是从配置读出来的、恒为真，于是永远
    #      跳过复用，每次都去要求密码。
    #   2. 不要用"会话年龄"来预判新鲜度 —— 本地时间不可靠，服务端才是
    #      权威。直接拿它试一次接口，失败再登录，这样最稳。
    if cached_session and cached_session.token:
        verdict = check_session(
            cached_session,
            min_interval=cfg.poll.min_request_interval,
            timeout=cfg.http.timeout,
            verify=cfg.http.verify_ssl,
            proxy=cfg.http.proxy or None,
        )
        if verdict is SessionCheck.UNKNOWN:
            # 网络抖动 / 被限流不是登录问题 —— 不能因此把会话丢掉并要求
            # 重新输密码（实测踩过：一次 SSL EOF 就触发了重新登录流程，
            # 把好好的登录态扔了）。直接上抛，让用户重试。
            raise BitxkError(
                "校验本地登录态时网络异常。\n登录态本身没有失效（已保留缓存），请检查网络后重试。"
            )
        if verdict is SessionCheck.VALID:
            http.cookies = dict(cached_session.cookies)
            http.set_token(cached_session.token)
            http.student_code = cached_session.student_code
            log_ok(
                f"复用本地缓存会话（{cached_session.student_name or cached_session.student_code}）"
            )
            return http, _client(cfg, http), cached_session
        logger.debug("缓存会话已失效，改为重新登录")

    if not need_login:
        return http, _client(cfg, http), Session(token="")

    # 走到这里说明要真正登录；若配置里没学号而缓存会话里有，就补上，
    # 免得后面拿不到 studentCode 而无从查询
    session_hint = cached_session.student_code if cached_session else ""
    if not cfg.username and session_hint:
        cfg.username = session_hint

    _resolve_credentials(cfg, args)
    auth = _build_auth(cfg, http, args)
    log(f"正在登录统一身份认证（{cfg.username}）…")
    session = auth.login(cfg.username, cfg.password)
    http.cookies = session.cookies
    http.set_token(session.token)
    try:
        session.save(cfg.base_dir / cfg.session_file)
    except OSError as exc:
        log_warn(f"会话未能保存：{exc}")
    log_ok(f"登录成功：{session.student_name or session.student_code}")
    return http, _client(cfg, http), session


# --------------------------------------------------------------------------
# 子命令实现
# --------------------------------------------------------------------------


def cmd_init(args) -> int:
    target = Path(args.config) if args.config else Path.cwd() / DEFAULT_CONFIG_NAME
    if target.exists():
        log_err(f"{target} 已存在，未覆盖。如需重新生成请先手动删除。")
        return 1
    target.write_text(SAMPLE_CONFIG, encoding="utf-8")
    log_ok(f"已生成配置模板：{target}")
    _echo("")
    _echo("接下来：")
    _echo(f"  1. 编辑 {target.name}，填入学号密码与想选的课")
    _echo("  2. 运行 bitxk check   自检环境")
    _echo("  3. 运行 bitxk login   测试登录")
    _echo("  4. 运行 bitxk list    查看余量")
    _echo("  5. 运行 bitxk grab    开始抢课")
    return 0


def cmd_check(args) -> int:
    """不需要账号的自检：网络可达性 + 登录页结构。"""
    _echo(Style.bold("BIT 选课工具 环境自检"))
    _echo("")

    http = HttpClient(min_interval=0.5, timeout=15, max_retries=1)
    ok = True
    try:
        # 1. 选课系统首页
        log("检查选课系统首页…")
        resp = http.get(f"{API_BASE}/*default/index.do")
        if resp.status_code == 200:
            log_ok(f"选课系统可访问（HTTP {resp.status_code}）")
        else:
            log_err(f"选课系统返回 HTTP {resp.status_code}")
            ok = False

        # 2. 本科选课系统的 CAS 入口（首页里注入的 casUrl）
        log("检查选课系统 CAS 入口…")
        resp = http.get(f"{API_BASE}/bitXsxkLogin/casLogin.do", allow_redirects=False)
        location = resp.headers.get("Location", "")
        if resp.status_code in (301, 302, 303, 307, 308) and "sso.bit.edu.cn/cas/login" in location:
            log_ok("CAS 入口正常（302 → 统一身份认证）")
        elif resp.status_code in (301, 302, 303, 307, 308):
            log_warn(f"CAS 入口重定向到了非预期地址：{location[:80]}")
        else:
            log_warn(f"CAS 入口返回 HTTP {resp.status_code}（预期 302）")

        # 3. 统一身份认证登录页结构
        log("检查统一身份认证登录页结构…")
        url = f"{CAS_LOGIN_URL}?service=https%3A%2F%2Fxk.bit.edu.cn%2Fxsxkapp%2Fsys%2Fxsxkapp%2FbitXsxkLogin%2FcasLogin.do"
        resp = http.get(url)
        html = resp.text
        has_croypto = 'id="login-croypto"' in html
        has_flowkey = 'id="login-page-flowkey"' in html
        # 旧版 CAS 用 pwdEncryptSalt 承载盐值，id/name 两种写法都可能有
        is_legacy = "pwdEncryptSalt" in html and not has_croypto
        if has_croypto and has_flowkey:
            log_ok("登录页结构正常（找到 login-croypto / login-page-flowkey）")
        elif is_legacy:
            log_warn("检测到旧版 CAS 登录页，请加 --encrypt-mode cbc")
        else:
            log_err("登录页结构已变更，自动登录可能失效")
            _echo("     兜底方案：在浏览器里登录后，用 --cookie 与 --token 手动导入。")
            ok = False

        # 4. 加密依赖
        try:
            from Crypto.Cipher import AES  # noqa: F401

            log_ok("密码加密依赖 pycryptodome 就绪")
        except ImportError:
            log_err("缺少 pycryptodome：pip install pycryptodome")
            ok = False

        # 5. 网络出口
        log("检测网络环境…")
        log_ok("能直连学校服务器（校内网络或已挂学校 VPN）")

    except BitxkError as exc:
        log_err(f"网络检查失败：{exc}")
        ok = False
    finally:
        http.close()

    _echo("")
    if ok:
        _echo(Style.green("自检通过，可以继续使用。"))
        return 0
    _echo(Style.yellow("自检发现问题，请按上面的提示处理。"))
    return 1


def cmd_gui(args) -> int:
    """启动图形界面。"""
    try:
        from .gui import run_gui
    except ImportError as exc:
        # 精简版发行包不带 tkinter，这里要给出人话提示而不是崩栈
        log_err(f"这个版本没有图形界面支持（{exc}）。")
        _echo("")
        _echo("请改用命令行：")
        _echo("  bitxk browser-login   用浏览器登录")
        _echo("  bitxk list            查看余量")
        _echo("  bitxk grab            开始抢课")
        return 4

    log("正在启动图形界面…（关闭窗口即退出）")
    return run_gui(args.config)


def cmd_list_browsers(args) -> int:
    """列出本机可用的 Chromium 系浏览器。"""
    from .browser import default_profile_dir, detect_browsers

    _echo(Style.bold("本机检测到的 Chromium 系浏览器"))
    _echo("")
    browsers = detect_browsers(getattr(args, "browser", None) or None)
    if not browsers:
        log_err("没有找到任何 Chromium 系浏览器。")
        _echo("")
        _echo("解决方式（任选其一）：")
        _echo("  1. 安装 Google Chrome / Microsoft Edge / Chromium")
        _echo("  2. 设置环境变量 BITXK_BROWSER 指向浏览器可执行文件")
        _echo("  3. 用 --browser <路径> 手工指定")
        return 1

    for index, item in enumerate(browsers):
        mark = Style.green("  ← 默认使用") if index == 0 else ""
        _echo(f"  {index + 1}. {item.name}  {Style.dim('[' + item.source + ']')}{mark}")
        _echo(f"     {Style.dim(str(item.path))}")
    _echo("")
    _echo(f"浏览器登录会使用的 profile 目录：{Style.dim(str(default_profile_dir()))}")
    _echo("（该目录用于记住登录态，删掉它即等于退出登录）")
    return 0


def cmd_browser_login(args) -> int:
    """浏览器登录一次，把登录态存下来。"""
    # 这个子命令本身就意味着"用浏览器登录"，不必再让用户加 --browser。
    # 空串是与 --browser 不带值相同的语义：用自动检测到的浏览器。
    if getattr(args, "browser", None) is None:
        args.browser = ""

    cfg = load_config(args.config)
    # 浏览器登录不需要账号密码；学号可选（给了就能顺手校验学籍）
    if getattr(args, "student_code", None):
        cfg.username = args.student_code
    cfg.validate(require_account=False)

    http, client, session = _connect(cfg, args)
    try:
        log_ok("登录态已缓存，可以直接运行 bitxk grab 了。")
        try:
            info = client.student_info(session.student_code)
            name = info.get("name") or session.student_name
            campus = info.get("campusName") or ""
            log(f"  身份：{name}（{session.student_code}）{campus}")
            for batch in client.batches(session.student_code):
                marker = Style.green("  ← 当前可选") if batch.can_select else ""
                log(f"  {batch}{marker}")
        except BitxkError as exc:
            log_warn(f"（无法读取批次信息：{exc}）")
        return 0
    finally:
        http.close()


def cmd_login(args) -> int:
    cfg = load_config(args.config)
    cfg.validate(require_account=not _has_usable_session(cfg, args))
    http, client, session = _connect(cfg, args)
    try:
        info = client.student_info()
        name = info.get("name") or info.get("xm") or session.student_name
        code = info.get("number") or info.get("xh") or session.student_code
        log_ok(f"身份确认：{name}（{code}）")
        for batch in client.batches():
            marker = Style.green("← 当前可选") if batch.can_select else ""
            log(f"  {batch} {marker}")
        return 0
    finally:
        http.close()


def cmd_list(args) -> int:
    cfg = load_config(args.config)
    cfg.validate(require_account=not _has_usable_session(cfg, args))
    http, client, session = _connect(cfg, args)
    try:
        batch = client.current_batch()
        log_ok(f"当前批次：{batch}")
        student_code = session.student_code or cfg.username

        _echo("")
        _echo(Style.bold(f"{'课程':<20} {'教学班':<12} {'教师':<10} {'容量':<16} 状态"))
        _echo(Style.dim("─" * 78))
        for target in cfg.enabled_courses:
            try:
                classes = client.find_teaching_classes(
                    target.name,
                    teaching_class_type=target.type,
                    batch_code=batch.code,
                    student_code=student_code,
                )
            except (TokenExpired, NotInBatchError) as exc:
                log_err(f"[{target.name}] {exc}")
                continue
            classes = [tc for tc in classes if target.matches(tc)]
            if not classes:
                _echo(f"{target.name:<20} {Style.dim('未找到匹配的教学班')}")
                continue
            for tc in classes:
                color = {
                    CourseStatus.AVAILABLE: Style.green,
                    CourseStatus.FULL: Style.dim,
                    CourseStatus.SELECTED: Style.cyan,
                    CourseStatus.CONFLICT: Style.yellow,
                }.get(tc.status, str)
                _echo(
                    f"{target.name:<20} {tc.teaching_class_id:<12} "
                    f"{(tc.teacher or '-'):<10} {tc.capacity_text:<16} "
                    f"{color(tc.status.label)}"
                )
        return 0
    finally:
        http.close()


def cmd_grab(args) -> int:
    cfg = load_config(args.config)

    # 命令行覆盖配置
    if args.interval is not None:
        cfg.poll.interval = args.interval
    if args.duration is not None:
        cfg.poll.max_duration = args.duration
    if args.dry_run:
        cfg.notify.stop_on_success = False

    manual = _manual_session(args, cfg)
    cfg.validate(require_account=not _has_usable_session(cfg, args))

    http: HttpClient | None = None
    try:
        if _use_browser_login(args):
            http = _build_http(cfg)
            session = browser_login_session(cfg, args)
            session.save(cfg.base_dir / cfg.session_file)
            http.cookies = session.cookies
            http.set_token(session.token)
            http.student_code = session.student_code
            client = _client(cfg, http)
        elif manual is not None:
            http = _build_http(cfg)
            http.cookies = manual.cookies
            http.set_token(manual.token)
            if manual.student_code:
                http.student_code = manual.student_code
            client = _client(cfg, http)
            session = manual
            if not manual.student_code:
                log_warn(
                    "手动导入模式未提供学号，将无法查询批次。"
                    "请用 --student-code 指定，或在 config.toml 里填 username。"
                )
            log_ok("已使用手动导入的登录态")
        else:
            http, client, session = _connect(cfg, args)

        auth = _build_auth(cfg, http, args)
        notifier = Notify(sound=cfg.notify.sound)

        if args.dry_run:
            log_warn("dry-run 模式：只会查询余量，不会提交任何选课请求")

        poller = Poller(
            cfg,
            auth,
            client,
            http,
            session=session,
            on_event=_make_renderer(notifier, dry_run=args.dry_run),
            dry_run=args.dry_run,
        )

        # Ctrl+C 优雅退出
        import signal

        def _on_sigint(_sig, _frame):
            _echo("")
            log_warn("收到中断信号，正在停止…")
            poller.stop()

        # 非主线程无法注册信号处理器，忽略即可
        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGINT, _on_sigint)

        stats = poller.run()
        _echo("")
        _echo(Style.bold("运行统计"))
        _echo(f"  {stats.summary()}")
        if not poller.started:
            return 5
        return 0 if stats.successes > 0 or args.dry_run else 1

    except CaptchaRequired as exc:
        log_err(str(exc))
        return 2
    except LoginError as exc:
        log_err(f"登录失败：{exc}")
        _echo("")
        _echo("排查建议：")
        _echo("  1. 确认学号密码正确（可先在浏览器登录一次验证）")
        _echo("  2. 若浏览器能登而脚本不能，试 --encrypt-mode cbc")
        _echo("  3. 仍不行就用兜底方案：浏览器 F12 复制 Cookie 与 Token，")
        _echo("     然后运行 bitxk grab --cookie '...' --token '...'")
        return 2
    except NotInBatchError as exc:
        log_err(str(exc))
        return 3
    except ConfigError as exc:
        log_err(str(exc))
        return 4
    except BitxkError as exc:
        log_err(str(exc))
        return 5
    finally:
        if http is not None:
            http.close()


# --------------------------------------------------------------------------
# 事件渲染
# --------------------------------------------------------------------------


def _make_renderer(notifier: Notify, *, dry_run: bool = False):
    """把轮询引擎的事件渲染成终端输出。"""

    def render(event: str, payload: dict) -> None:
        if event == "login":
            log(f"正在登录统一身份认证（{payload.get('username')}）…")
        elif event == "login_ok":
            name = payload.get("name") or ""
            log_ok(f"登录成功：{name}（{payload.get('code')}）")
        elif event == "batch":
            log_ok(f"当前可选批次：{payload.get('batch')}")
            _echo("")
        elif event == "start":
            n = payload.get("courses", 0)
            log(f"开始轮询 {n} 门课程。按 Ctrl+C 可随时停止。")
            _echo("")
        elif event == "status":
            _render_status(payload)
        elif event == "attempt":
            log(f"  → 尝试选课 {payload.get('course')} / {payload.get('class_id')}")
        elif event == "dry_run_skip":
            log(
                f"  → [dry-run] 发现余量：{payload.get('course')} / "
                f"{payload.get('class_id')} {payload.get('capacity')}，未提交"
            )
        elif event == "success":
            notifier.success(str(payload.get("course")), str(payload.get("message", "")))
            _echo("")
            log_ok(
                Style.bold(
                    f"选课成功：{payload.get('course')} "
                    f"（教学班 {payload.get('class_id')} {payload.get('teacher') or ''}）"
                )
            )
            _echo("")
        elif event == "already":
            log_ok(f"已经选过这门课：{payload.get('course')}")
        elif event == "conflict":
            log_warn(
                f"时间冲突，跳过该教学班：{payload.get('course')} / "
                f"{payload.get('class_id')} —— {payload.get('message')}"
            )
        elif event == "miss":
            outcome = payload.get("outcome")
            message = payload.get("message") or ""
            prefix = Style.dim("  ·")
            if outcome == "full":
                log(f"{prefix} {payload.get('course')} 容量已满，继续等待 —— {message}")
            else:
                log(f"{prefix} {payload.get('course')} {message}")
        elif event == "relogin":
            log_warn("登录态失效，正在重新登录…")
        elif event == "server_busy":
            log_warn(
                f"选课系统在线人数已达上限，冷却 {payload.get('cooldown')} 秒后重试"
                f"（{payload.get('message') or ''}）"
            )
        elif event == "rate_limited":
            log_warn(
                f"被服务端限流，冷却 {payload.get('cooldown')} 秒；"
                f"轮询间隔调整为 {payload.get('interval')} 秒"
            )
        elif event == "warn":
            log_warn(str(payload.get("message")))
        elif event == "error":
            log_err(str(payload.get("message")))
        elif event == "timeout":
            log_warn(f"已达到最长运行时间（{payload.get('seconds')} 秒），退出。")
        elif event == "all_done":
            log_ok("所有目标课程都已处理完成。")
        elif event == "interrupted":
            log_warn("已手动停止。")
        elif event == "fatal":
            log_err(str(payload.get("message")))
        elif event == "finish":
            pass

    def _render_status(payload: dict) -> None:
        classes = payload.get("classes") or []
        course = payload.get("course", "")
        if not classes:
            log_warn(f"  {course}：未找到匹配的教学班")
            return
        for item in classes:
            status = item.get("status")
            if status == "available":
                text = Style.green(
                    f"  {course} / {item.get('id')} {item.get('teacher') or ''} "
                    f"{item.get('capacity')} ★有余量"
                )
            elif status == "full":
                text = Style.dim(
                    f"  {course} / {item.get('id')} {item.get('teacher') or ''} "
                    f"{item.get('capacity')}"
                )
            else:
                text = Style.yellow(
                    f"  {course} / {item.get('id')} {item.get('teacher') or ''} "
                    f"{item.get('capacity')} {item.get('status_label')}"
                )
            log(text)

    return render


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _setup_logging(args)

    # --list-browsers 不依赖子命令，先处理掉
    if getattr(args, "list_browsers", False):
        return cmd_list_browsers(args)

    command = args.command
    if command is None:
        build_parser().print_help()
        return 0
    if command == "init":
        return cmd_init(args)
    if command == "check":
        return cmd_check(args)

    try:
        if command == "login":
            return cmd_login(args)
        if command == "list":
            return cmd_list(args)
        if command == "browser-login":
            return cmd_browser_login(args)
        if command == "gui":
            return cmd_gui(args)
        if command == "grab":
            if args.once:
                return cmd_list(args)
            return cmd_grab(args)
        build_parser().print_help()
        return 0
    except ConfigError as exc:
        log_err(str(exc))
        return 4
    except KeyboardInterrupt:
        _echo("")
        log_warn("已中断。")
        return 130
    except BitxkError as exc:
        log_err(str(exc))
        return 5


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
