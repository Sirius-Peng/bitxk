"""配置：TOML 文件 / 环境变量 / 命令行三层覆盖。

用标准库 ``tomllib`` 解析 TOML，不引入额外依赖（Python ≥ 3.11）。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .client import CourseType
from .exceptions import ConfigError

__all__ = [
    "WatchTarget",
    "PollConfig",
    "Config",
    "load_config",
    "DEFAULT_CONFIG_NAME",
    "SAMPLE_CONFIG",
]

DEFAULT_CONFIG_NAME = "config.toml"

#: 默认配置文件内容（``bitxk init`` 写出来的就是这个）
SAMPLE_CONFIG = """\
# BIT 选课助手配置文件
# 所有时间单位均为秒。

[account]
# 学号 / 密码也可以通过环境变量 BITXK_USERNAME、BITXK_PASSWORD 提供，
# 那样更安全（不会把密码写进文件）。
username = ""
password = ""

[poll]
# 轮询间隔（秒）。请不要设得太小：这是别人学校的生产服务器，
# 而且请求过于频繁更容易被风控。1.5~3 秒是合理区间。
interval = 2.0

# 每次请求之间的最小间隔（秒），用于多课程并发时的全局限速。
min_request_interval = 0.8

# 间隔抖动（秒）：实际等待 = interval + random(0, jitter)，
# 避免所有客户端在同一毫秒发出请求。
jitter = 0.5

# 最大轮询时长（秒）。0 表示不限制。默认 4 小时。
max_duration = 14400

# 连续失败多少次后退出（网络恢复后计数会归零）。
max_consecutive_errors = 30

# 被限流时的冷却时间（秒）。
rate_limit_cooldown = 30.0

# 会话使用多久后主动重新登录（秒）。选课系统 token 大约 15~20 分钟失效。
relogin_after = 600

[http]
timeout = 10.0
max_retries = 3
# 校外访问可填校园 WebVPN 代理，例如 "http://127.0.0.1:8080"
proxy = ""
verify_ssl = true

[notify]
# 抢到课 / 程序出错时的终端提示音
sound = true
# 抢到课后是否自动停止（true = 只抢第一门成功的课就退出）
stop_on_success = false

# ---------------------------------------------------------------
# 要盯的课程。可以写多门，工具会并发轮询（受 min_request_interval 限速）。
#
# name        课程名，必须与选课系统里显示的完全一致
# type        教学班类型：XGXK 校公选课 / TYKC 体育 / FANKC 方案内 / TJKC 系统推荐
# priority    优先级，数字越小越先选（默认 100）
# teachers    只选指定老师的课（可选，填老师姓名的一部分即可）
# classes     只选指定教学班 ID（可选，多个用逗号分隔）
# enabled     是否启用（默认 true）
# ---------------------------------------------------------------

[[courses]]
name = "科幻文学"
type = "XGXK"
priority = 100
# teachers = ["张三"]
# classes = ["1234567"]
enabled = true

# [[courses]]
# name = "体育/羽毛球"
# type = "TYKC"
# priority = 50
# enabled = true
"""


# --------------------------------------------------------------------------
# 课程目标
# --------------------------------------------------------------------------


@dataclass
class WatchTarget:
    """一门要盯的课程。"""

    name: str
    type: str = CourseType.PUBLIC
    priority: int = 100
    teachers: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    enabled: bool = True

    def __post_init__(self) -> None:
        self.name = (self.name or "").strip()
        if not self.name:
            raise ConfigError("courses.name 不能为空")
        self.type = (self.type or CourseType.PUBLIC).strip().upper()
        if self.type not in CourseType.ALL:
            raise ConfigError(
                f"课程「{self.name}」的 type={self.type} 非法，可选值：{', '.join(CourseType.ALL)}"
            )
        # 允许 "老师A,老师B" 这种写法
        if isinstance(self.teachers, str):
            self.teachers = [t.strip() for t in self.teachers.split(",") if t.strip()]
        if isinstance(self.classes, str):
            self.classes = [c.strip() for c in self.classes.split(",") if c.strip()]
        self.teachers = [str(t).strip() for t in self.teachers if str(t).strip()]
        self.classes = [str(c).strip() for c in self.classes if str(c).strip()]

    @property
    def type_label(self) -> str:
        return CourseType.label(self.type)

    def matches(self, tc) -> bool:
        """判断某个教学班是否满足本目标的筛选条件。"""
        if self.classes:
            return str(tc.teaching_class_id) in self.classes
        if self.teachers:
            teacher = tc.teacher or ""
            return any(t in teacher for t in self.teachers)
        return True

    def __str__(self) -> str:
        extra = ""
        if self.teachers:
            extra += f" 老师={'/'.join(self.teachers)}"
        if self.classes:
            extra += f" 教学班={'/'.join(self.classes)}"
        return f"{self.name} ({self.type_label}, 优先级 {self.priority}){extra}"


# --------------------------------------------------------------------------
# 轮询策略
# --------------------------------------------------------------------------


@dataclass
class PollConfig:
    """轮询与限速策略。"""

    interval: float = 2.0
    min_request_interval: float = 0.8
    jitter: float = 0.5
    max_duration: float = 4 * 3600
    max_consecutive_errors: int = 30
    rate_limit_cooldown: float = 30.0
    relogin_after: float = 600.0

    def __post_init__(self) -> None:
        if self.interval < 0.5:
            raise ConfigError(
                f"poll.interval={self.interval} 太小。请勿低于 0.5 秒，"
                "建议 1.5~3 秒，避免给学校服务器造成压力并触发风控。"
            )
        if self.min_request_interval < 0.2:
            raise ConfigError("poll.min_request_interval 不得低于 0.2 秒")
        if self.max_consecutive_errors < 1:
            raise ConfigError("poll.max_consecutive_errors 必须大于 0")


@dataclass
class HttpConfig:
    timeout: float = 10.0
    max_retries: int = 3
    proxy: str = ""
    verify_ssl: bool = True


@dataclass
class NotifyConfig:
    sound: bool = True
    stop_on_success: bool = False


@dataclass
class Config:
    """完整配置。"""

    username: str = ""
    password: str = ""
    poll: PollConfig = field(default_factory=PollConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    courses: list[WatchTarget] = field(default_factory=list)
    #: 加密模式（SSO 页面结构变化时可切换）
    encrypt_mode: str = "ecb"
    #: 会话缓存路径
    session_file: str = ".bitxk_session.json"
    #: 配置文件所在目录（用于解析相对路径）
    base_dir: Path = field(default_factory=Path.cwd)

    @property
    def enabled_courses(self) -> list[WatchTarget]:
        """按优先级排序后的启用课程。"""
        return sorted(
            (c for c in self.courses if c.enabled),
            key=lambda c: (c.priority, c.name),
        )

    def validate(self, *, require_account: bool = True) -> None:
        """校验配置完整性。

        Args:
            require_account: 是否要求配置学号。手动导入登录态
                （``--token``）时不需要账号密码，此时传 ``False``。
        """
        if require_account and not self.username:
            raise ConfigError(
                "未配置学号。请在 config.toml 的 [account] 段填写 username，"
                "或设置环境变量 BITXK_USERNAME。"
            )
        if not self.courses:
            raise ConfigError("未配置任何课程。请在 config.toml 里添加 [[courses]] 段。")
        if not self.enabled_courses:
            raise ConfigError("所有课程都被禁用了（enabled = false），没有需要盯的课。")


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------


def _section(data: dict, name: str) -> dict:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def _build(cls, data: dict, *, strict: bool = True):
    """把 dict 转成 dataclass，忽略未知键（宽松）或报错（严格）。"""
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown and strict:
        raise ConfigError(
            f"{cls.__name__} 出现未知配置项：{', '.join(sorted(unknown))}。"
            f"可用项：{', '.join(sorted(known))}"
        )
    kwargs = {k: v for k, v in data.items() if k in known}
    return cls(**kwargs)


def load_config(path: str | Path | None = None) -> Config:
    """从 TOML 文件加载配置，并叠加环境变量覆盖。

    Args:
        path: 配置文件路径。``None`` 时在当前目录找 ``config.toml``。

    Raises:
        ConfigError: 文件不存在或内容非法。
    """
    if path is None:
        candidate = Path.cwd() / DEFAULT_CONFIG_NAME
        if not candidate.exists():
            raise ConfigError(f"找不到配置文件 {candidate}。请先运行 `bitxk init` 生成模板。")
        path = candidate
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件 {path} 语法错误：{exc}") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}：{exc}") from exc

    account = _section(raw, "account")
    cfg = Config(
        username=str(account.get("username", "") or ""),
        password=str(account.get("password", "") or ""),
        poll=_build(PollConfig, _section(raw, "poll")),
        http=_build(HttpConfig, _section(raw, "http")),
        notify=_build(NotifyConfig, _section(raw, "notify")),
        encrypt_mode=str(raw.get("encrypt_mode", "ecb") or "ecb"),
        session_file=str(raw.get("session_file", ".bitxk_session.json") or ".bitxk_session.json"),
        base_dir=path.parent,
    )

    courses_raw = raw.get("courses") or []
    if not isinstance(courses_raw, list):
        raise ConfigError("[[courses]] 必须是数组表（每门课一个 [[courses]] 段）")
    for index, item in enumerate(courses_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"第 {index + 1} 个 [[courses]] 段格式错误")
        try:
            cfg.courses.append(_build(WatchTarget, item))
        except ConfigError as exc:
            raise ConfigError(f"第 {index + 1} 个 [[courses]] 段：{exc}") from exc

    _apply_env_overrides(cfg)
    return cfg


def _apply_env_overrides(cfg: Config) -> None:
    """环境变量优先级高于配置文件。"""
    env_user = os.environ.get("BITXK_USERNAME")
    env_pass = os.environ.get("BITXK_PASSWORD")
    if env_user:
        cfg.username = env_user
    if env_pass:
        cfg.password = env_pass
    if os.environ.get("BITXK_INSECURE"):
        cfg.http.verify_ssl = False
