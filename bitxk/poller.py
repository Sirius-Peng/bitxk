"""轮询引擎：盯容量 → 有余量就抢 → 抢到就报。

核心循环
--------
每一轮做三件事：

1. **刷新一批课程状态**（按 ``min_request_interval`` 全局限速，串行发请求）；
2. 找出其中「有余量且满足筛选条件」的教学班，**按优先级排序**后依次尝试提交；
3. 睡眠 ``interval + jitter`` 后进入下一轮。

异常处理策略
------------
* ``TokenExpired`` → 重新登录，然后**立刻**继续（不浪费一整轮等待）；
* ``RateLimited``  → 冷却 ``rate_limit_cooldown`` 秒，并且自动把间隔乘 1.5（有上限）；
* 网络抖动       → 计入连续错误，超过阈值才退出；
* 抢课成功       → 记录并触发回调；``stop_on_success`` 为真时结束整轮。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .auth import BitAuth, Session
from .client import XkClient
from .config import Config, WatchTarget
from .exceptions import (
    ApiError,
    BitxkError,
    LoginError,
    NetworkError,
    NotInBatchError,
    RateLimited,
    TokenExpired,
)
from .http import HttpClient
from .models import CourseStatus, SelectionOutcome, SelectionResult, TeachingClass

logger = logging.getLogger(__name__)

__all__ = ["Poller", "PollStats", "EventHandler"]


EventHandler = Callable[[str, dict], None]
"""事件回调：``(event_name, payload)``。用于把进度推给 CLI / GUI。"""


@dataclass
class PollStats:
    """轮询统计，用于收尾汇报。"""

    rounds: int = 0
    queries: int = 0
    attempts: int = 0
    successes: int = 0
    relogins: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    rate_limited: int = 0
    started_at: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def summary(self) -> str:
        minutes, seconds = divmod(int(self.elapsed), 60)
        hours, minutes = divmod(minutes, 60)
        duration = f"{hours}小时{minutes}分{seconds}秒" if hours else f"{minutes}分{seconds}秒"
        return (
            f"运行 {duration} | 轮次 {self.rounds} | 查询 {self.queries} | "
            f"提交 {self.attempts} | 成功 {self.successes} | "
            f"重登 {self.relogins} | 限流 {self.rate_limited} | 错误 {self.errors}"
        )


@dataclass
class _TargetState:
    """单个课程的运行时状态。"""

    target: WatchTarget
    classes: list[TeachingClass] = field(default_factory=list)
    last_error: str = ""
    resolved: bool = False
    #: 已确认时间冲突的教学班 ID。
    #: 必须记在这里而不是只标在教学班对象上 —— 每轮刷新都会用接口返回的
    #: 新对象替换 ``classes``，对象上的标记会被冲掉，导致同一门冲突课被反复提交。
    conflicted: set[str] = field(default_factory=set)

    @property
    def available(self) -> list[TeachingClass]:
        return [
            tc
            for tc in self.classes
            if tc.is_selectable and tc.teaching_class_id not in self.conflicted
        ]


class Poller:
    """把「配置 + 登录态」变成「持续轮询并自动选课」的引擎。"""

    def __init__(
        self,
        config: Config,
        auth: BitAuth,
        client: XkClient,
        http: HttpClient,
        *,
        session: Session | None = None,
        on_event: EventHandler | None = None,
        stop_event: threading.Event | None = None,
        dry_run: bool = False,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.auth = auth
        self.client = client
        self.http = http
        self.session = session
        self.on_event = on_event
        self.stop_event = stop_event or threading.Event()
        #: 只观察不提交 —— 用于安全试跑与排查
        self.dry_run = dry_run
        #: 睡眠实现，测试时可注入以便瞬间跑完多轮
        self._sleep_impl: Callable[[float], None] = sleep or self._default_sleep

        self.stats = PollStats()
        self.batch_code: str = ""
        self.student_code: str = config.username
        self.states: dict[str, _TargetState] = {
            t.name: _TargetState(target=t) for t in config.enabled_courses
        }
        #: 是否成功完成启动（登录 + 确定批次）。
        #: 启动失败时不应被当成"正常跑完"，否则调用方会误判退出码。
        self.started = False

        self._interval = config.poll.interval
        self._consecutive_errors = 0

    # ================================================================ 对外接口

    def run(self) -> PollStats:
        """开始轮询，直到所有课都抢到 / 超时 / 出错 / 被外部停止。"""
        try:
            self._bootstrap()
        except NotInBatchError as exc:
            self._emit("fatal", message=str(exc))
            return self.stats
        except BitxkError as exc:
            self._emit("fatal", message=f"启动失败：{exc}")
            return self.stats

        deadline = (
            time.time() + self.config.poll.max_duration
            if self.config.poll.max_duration > 0
            else None
        )

        self._emit("start", batch=str(self.batch_code), courses=len(self.states))
        self.started = True

        while not self.stop_event.is_set():
            if deadline is not None and time.time() >= deadline:
                self._emit("timeout", seconds=self.config.poll.max_duration)
                break
            if self._all_resolved():
                self._emit("all_done")
                break

            try:
                self._round()
            except KeyboardInterrupt:
                self._emit("interrupted")
                break
            except LoginError as exc:
                # 账号密码错误重试没有意义，直接退出，避免锁账号
                self._emit("fatal", message=f"登录失败，已停止：{exc}")
                break

            if self._all_resolved() and self.config.notify.stop_on_success:
                self._emit("all_done")
                break

            self._sleep(self._next_delay())

        self._emit("finish", summary=self.stats.summary())
        return self.stats

    def stop(self) -> None:
        """请求停止轮询（线程安全）。"""
        self.stop_event.set()

    # ================================================================ 启动

    def _bootstrap(self) -> None:
        """登录（若还没登录）并确定当前批次。"""
        if self.session is None:
            self._login()
        assert self.session is not None
        self.http.set_token(self.session.token)
        self.student_code = self.session.student_code or self.config.username

        batch = self.client.current_batch()
        self.batch_code = batch.code
        self._emit("batch", batch=str(batch))

    def _login(self) -> None:
        """走一遍统一身份认证。"""
        cfg = self.config
        if not cfg.username or not cfg.password:
            raise LoginError(
                "缺少学号或密码。请在 config.toml 的 [account] 段填写，"
                "或设置环境变量 BITXK_USERNAME / BITXK_PASSWORD。"
            )
        self._emit("login", username=cfg.username)
        try:
            session = self.auth.login(cfg.username, cfg.password)
        except Exception:
            self.stats.errors += 1
            raise
        self.session = session
        self.http.set_token(session.token)
        self.http.cookies = session.cookies
        self._save_session()
        self._emit(
            "login_ok",
            name=session.student_name,
            code=session.student_code,
        )

    def _save_session(self) -> None:
        if not self.session:
            return
        path = self.config.base_dir / self.config.session_file
        try:
            self.session.save(path)
        except OSError as exc:  # pragma: no cover
            logger.debug("会话保存失败：%s", exc)

    # ================================================================ 单轮

    def _round(self) -> None:
        """执行一轮：刷新状态 → 尝试选课。"""
        self.stats.rounds += 1
        self._refresh_all()

        # 收集有余量的教学班，按课程优先级排序后依次尝试
        pending: list[tuple[int, _TargetState, TeachingClass]] = []
        for state in self.states.values():
            if state.resolved:
                continue
            for tc in state.available:
                pending.append((state.target.priority, state, tc))
        pending.sort(key=lambda item: (item[0], item[2].teaching_class_id))

        for _priority, state, tc in pending:
            if self.stop_event.is_set():
                return
            # 抢到一门就收工（可配置），后续课程不再尝试
            if self._try_select(state, tc) and self.config.notify.stop_on_success:
                return

    def _refresh_all(self) -> None:
        """刷新所有未完成课程的状态。"""
        for state in self.states.values():
            if state.resolved or self.stop_event.is_set():
                continue
            self._refresh(state)

    def _refresh(self, state: _TargetState) -> None:
        """查询一门课的所有教学班并更新状态。"""
        target = state.target
        try:
            classes = self.client.find_teaching_classes(
                target.name,
                teaching_class_type=target.type,
                batch_code=self.batch_code,
                student_code=self.student_code,
            )
        except TokenExpired:
            self._relogin()
            return
        except RateLimited:
            self._handle_rate_limit()
            return
        except NotInBatchError as exc:
            state.last_error = str(exc)
            self._emit("warn", message=f"[{target.name}] 批次不可用：{exc}")
            return
        except (NetworkError, ApiError) as exc:
            state.last_error = str(exc)
            self._note_error(f"[{target.name}] 查询失败：{exc}")
            return

        self.stats.queries += 1
        self._note_success()

        filtered = [tc for tc in classes if target.matches(tc)]
        # 把之前已确认冲突的教学班重新标上，保证状态展示与实际策略一致
        for tc in filtered:
            if tc.teaching_class_id in state.conflicted:
                tc.status = CourseStatus.CONFLICT
        state.classes = filtered
        state.last_error = "" if filtered else "未匹配到教学班"

        if (
            filtered
            and not state.available
            and all(tc.status is CourseStatus.CONFLICT for tc in filtered)
        ):
            # 所有匹配到的教学班都冲突 —— 这门课再轮询下去也没意义
            state.resolved = True
            self._emit(
                "conflict",
                course=target.name,
                class_id=",".join(tc.teaching_class_id for tc in filtered),
                message="所有匹配的教学班都与已选课程时间冲突，已停止轮询该课",
            )

        self._emit(
            "status",
            course=target.name,
            type=target.type,
            classes=[
                {
                    "id": tc.teaching_class_id,
                    "teacher": tc.teacher,
                    "capacity": tc.capacity_text,
                    "remaining": tc.remaining,
                    "status": tc.status.value,
                    "status_label": tc.status.label,
                }
                for tc in filtered
            ],
        )

    # ================================================================ 选课

    def _try_select(self, state: _TargetState, tc: TeachingClass) -> bool:
        """尝试选一个教学班；返回是否成功。"""
        target = state.target

        if self.dry_run:
            self._emit(
                "dry_run_skip",
                course=target.name,
                class_id=tc.teaching_class_id,
                capacity=tc.capacity_text,
            )
            return False

        self.stats.attempts += 1
        self._emit("attempt", course=target.name, class_id=tc.teaching_class_id)
        try:
            result = self.client.submit(
                tc.teaching_class_id,
                batch_code=self.batch_code,
                student_code=self.student_code,
                teaching_class_type=target.type,
                course_name=target.name,
            )
        except TokenExpired:
            self._relogin()
            return False
        except RateLimited:
            self._handle_rate_limit()
            return False
        except (NetworkError, ApiError) as exc:
            self._note_error(f"[{target.name}] 提交失败：{exc}")
            return False

        return self._handle_result(state, tc, result)

    def _handle_result(
        self, state: _TargetState, tc: TeachingClass, result: SelectionResult
    ) -> bool:
        """处理选课结果。"""
        outcome = result.outcome
        self._note_success()

        if outcome is SelectionOutcome.SUCCESS:
            self.stats.successes += 1
            state.resolved = True
            self._emit(
                "success",
                course=state.target.name,
                class_id=tc.teaching_class_id,
                teacher=tc.teacher,
                message=result.message,
            )
            return True

        if outcome is SelectionOutcome.ALREADY:
            state.resolved = True
            self._emit(
                "already",
                course=state.target.name,
                class_id=tc.teaching_class_id,
                message=result.message,
            )
            return True

        if outcome is SelectionOutcome.CONFLICT:
            # 时间冲突不会因为等待而消失，记在黑名单里，避免每轮重复提交
            state.conflicted.add(tc.teaching_class_id)
            tc.status = CourseStatus.CONFLICT
            self._emit(
                "conflict",
                course=state.target.name,
                class_id=tc.teaching_class_id,
                message=result.message,
            )
            return False

        if outcome is SelectionOutcome.AUTH_ERROR:
            self._relogin()
            return False

        if outcome is SelectionOutcome.RATE_LIMITED:
            self._handle_rate_limit()
            return False

        # FULL / NOT_IN_BATCH / ERROR：保持轮询
        self._emit(
            "miss",
            course=state.target.name,
            class_id=tc.teaching_class_id,
            outcome=outcome.value,
            message=result.message,
        )
        return False

    # ================================================================ 故障恢复

    def _relogin(self) -> None:
        """重新登录并刷新批次信息。"""
        self.stats.relogins += 1
        self._emit("relogin")
        self.session = None
        self.http.set_token(None)
        try:
            self._login()
        except BitxkError as exc:
            self._note_error(f"重新登录失败：{exc}")
            return

        try:
            batch = self.client.current_batch()
            if batch.code != self.batch_code:
                self.batch_code = batch.code
                self._emit("batch", batch=str(batch))
        except BitxkError as exc:
            self._emit("warn", message=f"刷新批次失败：{exc}")

    def _handle_rate_limit(self) -> None:
        """被限流：冷却并把轮询间隔调大一些（有上限）。"""
        self.stats.rate_limited += 1
        cooldown = self.config.poll.rate_limit_cooldown
        # 自适应退让：最多放大到 8 倍
        self._interval = min(self._interval * 1.5, self.config.poll.interval * 8)
        self._emit("rate_limited", cooldown=cooldown, interval=round(self._interval, 2))
        self._sleep(cooldown)

    def _note_error(self, message: str) -> None:
        self.stats.errors += 1
        self._consecutive_errors += 1
        self.stats.consecutive_errors = self._consecutive_errors
        self._emit("error", message=message, consecutive=self._consecutive_errors)
        if self._consecutive_errors >= self.config.poll.max_consecutive_errors:
            self._emit(
                "fatal",
                message=(
                    f"连续 {self._consecutive_errors} 次失败，已停止。"
                    "请检查网络、账号状态或选课系统是否可访问。"
                ),
            )
            self.stop_event.set()

    def _note_success(self) -> None:
        self._consecutive_errors = 0
        self.stats.consecutive_errors = 0

    # ================================================================ 杂项

    def _all_resolved(self) -> bool:
        return bool(self.states) and all(s.resolved for s in self.states.values())

    def _next_delay(self) -> float:
        jitter = self.config.poll.jitter
        return self._interval + (random.uniform(0, jitter) if jitter > 0 else 0.0)

    def _sleep(self, seconds: float) -> None:
        """可被 stop() 立即打断的睡眠（测试时可替换实现）。"""
        if self.stop_event.is_set():
            return
        self._sleep_impl(seconds)

    def _default_sleep(self, seconds: float) -> None:
        self.stop_event.wait(max(0.0, seconds))

    def _emit(self, event: str, **payload) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event, payload)
        except Exception as exc:  # pragma: no cover - 回调不应影响主流程
            logger.debug("事件回调异常：%s", exc)
