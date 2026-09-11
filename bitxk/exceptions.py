"""异常体系。

分层的目的是让轮询引擎能针对不同故障采取不同策略：
登录失效要重登，网络抖动要退避重试，配置错误要直接退出。
"""

from __future__ import annotations

__all__ = [
    "BitxkError",
    "ConfigError",
    "NetworkError",
    "LoginError",
    "CaptchaRequired",
    "TokenExpired",
    "ApiError",
    "RateLimited",
    "NotInBatchError",
    "ServerBusy",
]


class BitxkError(Exception):
    """所有本工具异常的基类。"""


class ConfigError(BitxkError):
    """配置错误（缺字段、字段非法、文件不存在等）。不可重试。"""


class NetworkError(BitxkError):
    """网络层错误：超时、连接失败、DNS 解析失败等。可重试。"""


class LoginError(BitxkError):
    """统一身份认证失败（账号或密码错误）。不可重试，重试会锁账号。"""


class CaptchaRequired(LoginError):
    """SSO 要求输入验证码。

    通常意味着密码已被错误尝试多次，或触发了风控。
    本工具不自动破解验证码，应停止登录并提示用户手动处理。
    """


class TokenExpired(BitxkError):
    """选课系统登录态失效，需要重新走一遍认证流程。可重试（重登后继续）。"""


class ApiError(BitxkError):
    """业务接口返回了非预期结构或明确错误。"""

    def __init__(self, message: str, *, code=None, payload=None):
        super().__init__(message)
        self.code = code
        self.payload = payload


class RateLimited(BitxkError):
    """被服务端限流。应当显著放慢轮询频率。"""


class ServerBusy(BitxkError):
    """选课系统当前在线人数已达上限，暂时拒绝新会话。

    本科选课系统的信封里用 ``code == "4"`` 表示这种情况，前端提示
    「在线人数超过上限，请稍后再试！」。这不是账号或密码问题，
    等一会儿重试即可，所以**绝不能当成致命错误退出**。
    """


class NotInBatchError(BitxkError):
    """当前不在任何可选课批次内（选课未开始 / 已结束）。"""
