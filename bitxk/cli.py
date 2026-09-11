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

from . import __version__
from .auth import API_BASE, CAS_LOGIN_URL, BitAuth, Session
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


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


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


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def log(message: str) -> None:
    print(f"{Style.dim(_stamp())} {message}", flush=True)


def log_ok(message: str) -> None:
    log(f"{Style.green('✓')} {message}")


def log_warn(message: str) -> None:
    log(f"{Style.yellow('!')} {message}")


def log_err(message: str) -> None:
    log(f"{Style.red('✗')} {message}")


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
            "  bitxk grab                     开始轮询抢课\n"
            "  bitxk grab --interval 3 -v     指定间隔并输出调试日志\n"
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

    grab = sub.add_parser("grab", help="轮询并自动选课")
    _add_common_options(grab, prefixed=True)
    grab.add_argument("--once", action="store_true", help="只查一轮余量后退出（等同于 list）")
    return parser


def _merge_common(args) -> None:
    """把子命令上解析到的通用选项回填到主命名空间（子命令优先）。"""
    for name in ("cookie", "token", "encrypt_mode", "dry_run", "interval", "duration"):
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


def _resolve_credentials(cfg: Config, args) -> None:
    """按 命令行 > 环境变量 > 配置文件 > 交互输入 的优先级确定账号密码。"""
    if args.username:
        cfg.username = args.username
    if args.password:
        cfg.password = args.password

    if cfg.username and not cfg.password:
        cfg.password = _getpass(f"请输入 {cfg.username} 的密码：")
    elif not cfg.username:
        cfg.username = input("请输入学号：").strip()
        if cfg.username and not cfg.password:
            cfg.password = _getpass("请输入密码：")


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


def _manual_session(args, cfg: Config) -> Session | None:
    """如果用户给了 --token/--cookie，就构造手动会话。"""
    if not (args.token or args.cookie):
        return None
    session = BitAuth.from_manual(
        token=args.token or "",
        cookie=args.cookie or "",
        student_code=cfg.username,
    )
    if not session.token:
        raise ConfigError(
            "手动导入模式需要 --token（选课系统 Token 或带 bitXsxkLogin= 的回跳 URL）。"
            "只给 --cookie 无法定位登录态。"
        )
    return session


def _has_manual_session(args) -> bool:
    """是否走手动导入登录态的路径。"""
    return bool(getattr(args, "token", None) or getattr(args, "cookie", None))


def _connect(cfg: Config, args, *, need_login: bool = True) -> tuple[HttpClient, XkClient, Session]:
    """装配 HttpClient / XkClient / Session。"""
    # 手动导入登录态时不需要账号密码，因此不校验 account 段
    manual = _manual_session(args, cfg)
    cfg.validate(require_account=manual is None)
    http = _build_http(cfg)

    if manual is not None:
        http.cookies = manual.cookies
        http.set_token(manual.token)
        return http, XkClient(http), manual

    # 复用缓存会话（仅在未显式要求重新登录时）
    cached = Session.load(cfg.base_dir / cfg.session_file)
    if cached and cached.is_probably_fresh() and not args.password and not args.username:
        probe = HttpClient(
            min_interval=cfg.poll.min_request_interval,
            timeout=cfg.http.timeout,
            max_retries=0,
            verify=cfg.http.verify_ssl,
            proxy=cfg.http.proxy or None,
        )
        probe.cookies = cached.cookies
        probe.set_token(cached.token)
        try:
            XkClient(probe).student_info()
        except BitxkError:
            logger.debug("缓存会话已失效，改为重新登录")
        else:
            log_ok(f"复用本地缓存会话（{cached.student_name or cached.student_code}）")
            return probe, XkClient(probe), cached
        finally:
            if probe is not http:
                probe.close()

    if not need_login:
        return http, XkClient(http), Session(token="")

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
    return http, XkClient(http), session


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
    print()
    print("接下来：")
    print(f"  1. 编辑 {target.name}，填入学号密码与想选的课")
    print("  2. 运行 bitxk check   自检环境")
    print("  3. 运行 bitxk login   测试登录")
    print("  4. 运行 bitxk list    查看余量")
    print("  5. 运行 bitxk grab    开始抢课")
    return 0


def cmd_check(args) -> int:
    """不需要账号的自检：网络可达性 + 登录页结构。"""
    print(Style.bold("BIT 选课工具 环境自检"))
    print()

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

        # 2. 统一身份认证登录页结构
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
            print("     兜底方案：在浏览器里登录后，用 --cookie 与 --token 手动导入。")
            ok = False

        # 3. 加密依赖
        try:
            from Crypto.Cipher import AES  # noqa: F401

            log_ok("密码加密依赖 pycryptodome 就绪")
        except ImportError:
            log_err("缺少 pycryptodome：pip install pycryptodome")
            ok = False

        # 4. 网络出口（判断是否需要 WebVPN）
        log("检测网络环境…")
        log_ok("能连通学校服务器，当前处于可直连环境")

    except BitxkError as exc:
        log_err(f"网络检查失败：{exc}")
        ok = False
    finally:
        http.close()

    print()
    if ok:
        print(Style.green("自检通过，可以继续使用。"))
        return 0
    print(Style.yellow("自检发现问题，请按上面的提示处理。"))
    return 1


def cmd_login(args) -> int:
    cfg = load_config(args.config)
    cfg.validate()
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
    cfg.validate()
    http, client, session = _connect(cfg, args)
    try:
        batch = client.current_batch()
        log_ok(f"当前批次：{batch}")
        student_code = session.student_code or cfg.username

        print()
        print(Style.bold(f"{'课程':<20} {'教学班':<12} {'教师':<10} {'容量':<16} 状态"))
        print(Style.dim("─" * 78))
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
                print(f"{target.name:<20} {Style.dim('未找到匹配的教学班')}")
                continue
            for tc in classes:
                color = {
                    CourseStatus.AVAILABLE: Style.green,
                    CourseStatus.FULL: Style.dim,
                    CourseStatus.SELECTED: Style.cyan,
                    CourseStatus.CONFLICT: Style.yellow,
                }.get(tc.status, str)
                print(
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
    cfg.validate(require_account=manual is None)

    http: HttpClient | None = None
    try:
        if manual is not None:
            http = _build_http(cfg)
            http.cookies = manual.cookies
            http.set_token(manual.token)
            client = XkClient(http)
            session = manual
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
            print()
            log_warn("收到中断信号，正在停止…")
            poller.stop()

        # 非主线程无法注册信号处理器，忽略即可
        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGINT, _on_sigint)

        stats = poller.run()
        print()
        print(Style.bold("运行统计"))
        print(f"  {stats.summary()}")
        if not poller.started:
            return 5
        return 0 if stats.successes > 0 or args.dry_run else 1

    except CaptchaRequired as exc:
        log_err(str(exc))
        return 2
    except LoginError as exc:
        log_err(f"登录失败：{exc}")
        print()
        print("排查建议：")
        print("  1. 确认学号密码正确（可先在浏览器登录一次验证）")
        print("  2. 若浏览器能登而脚本不能，试 --encrypt-mode cbc")
        print("  3. 仍不行就用兜底方案：浏览器 F12 复制 Cookie 与 Token，")
        print("     然后运行 bitxk grab --cookie '...' --token '...'")
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
            print()
        elif event == "start":
            n = payload.get("courses", 0)
            log(f"开始轮询 {n} 门课程。按 Ctrl+C 可随时停止。")
            print()
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
            print()
            log_ok(
                Style.bold(
                    f"选课成功：{payload.get('course')} "
                    f"（教学班 {payload.get('class_id')} {payload.get('teacher') or ''}）"
                )
            )
            print()
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
        print()
        log_warn("已中断。")
        return 130
    except BitxkError as exc:
        log_err(str(exc))
        return 5


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
