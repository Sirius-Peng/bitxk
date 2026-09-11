"""轮询引擎测试 —— 本工具最核心的逻辑。

用假的 auth/client 驱动，验证：
容量出现就抢、满员继续等、按优先级排序、登录失效自动重登、
冲突不再重试、限流自适应退避、dry-run 绝不提交。
"""

from __future__ import annotations

import threading

import pytest

from bitxk.config import Config, PollConfig, WatchTarget
from bitxk.exceptions import LoginError, NetworkError, RateLimited, TokenExpired
from bitxk.models import Batch, CourseStatus, SelectionOutcome, SelectionResult, TeachingClass
from bitxk.poller import Poller


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------

class FakeAuth:
    def __init__(self, fail_login: Exception | None = None):
        self.calls = 0
        self.fail_login = fail_login

    def login(self, username, password):
        self.calls += 1
        if self.fail_login:
            raise self.fail_login

        from bitxk.auth import Session

        return Session(token=f"tok{self.calls}", student_name="张三", student_code=username)


class FakeClient:
    """按脚本返回课程状态的假客户端。

    ``script`` 是「每轮课容量」的列表，每项形如
    ``{"课程名": [("教学班ID", 剩余容量, 老师), ...]}``；
    脚本跑完后沿用最后一项。
    """

    def __init__(self, script: list[dict], *, submit_results: list | None = None):
        self.script = script
        self.submit_results = list(submit_results or [])
        self.round = 0
        self.queries = 0
        self.submits: list[str] = []
        self.batch_calls = 0

    def current_batch(self):
        self.batch_calls += 1
        return Batch(code="B1", name="第一轮", can_select=True, school_term="2024-2025-1")

    def find_teaching_classes(self, keyword, *, teaching_class_type="", batch_code="", student_code="", **kw):
        self.queries += 1
        stage = self.script[min(self.round, len(self.script) - 1)]
        entries = stage.get(keyword, [])
        return [
            TeachingClass.from_api({
                "teachingClassID": tc_id,
                "courseName": keyword,
                "teacherName": teacher,
                "remainCapacity": remaining,
                "capacity": 40,
            })
            for tc_id, remaining, teacher in entries
        ]

    def submit(self, teaching_class_id, **kwargs):
        self.submits.append(teaching_class_id)
        if self.submit_results:
            result = self.submit_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return SelectionResult(
            outcome=SelectionOutcome.SUCCESS,
            message="选课成功",
            teaching_class_id=teaching_class_id,
        )

    def student_info(self):
        return {"name": "张三"}


class RoundAdvancingClient(FakeClient):
    """每轮刷新结束后自动前进一步脚本，模拟「余量在第 N 轮出现」。"""

    def find_teaching_classes(self, keyword, **kwargs):
        result = super().find_teaching_classes(keyword, **kwargs)
        return result

    def advance(self):
        self.round += 1


def make_config(courses, **poll_overrides):
    poll = PollConfig(
        interval=0.5,
        min_request_interval=0.2,
        jitter=0.0,
        max_duration=0,
        **poll_overrides,
    )
    cfg = Config(username="1120200001", password="pw", poll=poll, courses=courses)
    # 测试里默认「抢到就停」，与生产配置的默认值不同，
    # 这样断言"只提交一次"更直接；需要观察多轮的用例显式关掉。
    cfg.notify.stop_on_success = True
    return cfg


def run_poller(client, config, *, max_rounds=40, advance=True, **kwargs):
    """跑一个受控轮数的引擎，返回 ``(stats, events, http)``。

    * ``advance``：每轮结束后推进假客户端的脚本，用于复现
      「余量在第 N 轮才出现」以及「提交失败后下一轮再试」。
    * ``max_rounds``：硬性轮数上限，保证测试不会挂住。
    """

    class FakeHttp:
        def __init__(self):
            self.token = None

        def set_token(self, token):
            self.token = token

        @property
        def cookies(self):
            return {}

        @cookies.setter
        def cookies(self, value):
            pass

    events: list[tuple[str, dict]] = []
    http = FakeHttp()
    poller = Poller(
        config, FakeAuth(), client, http,
        on_event=lambda e, p: events.append((e, p)),
        sleep=lambda s: None,
        **kwargs,
    )

    original_round = poller._round

    def observed_round():
        original_round()
        if advance and isinstance(client, RoundAdvancingClient):
            client.advance()
        if poller.stats.rounds >= max_rounds:
            poller.stop()

    poller._round = observed_round  # type: ignore[method-assign]
    stats = poller.run()
    return stats, events, http


def event_names(events):
    return [name for name, _ in events]


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------

class TestBasicFlow:
    def test_有余量立刻抢并成功(self):
        client = FakeClient([{"科幻文学": [("1001", 5, "张三")]}])
        cfg = make_config([WatchTarget(name="科幻文学")])
        stats, events, _ = run_poller(client, cfg)

        assert stats.successes == 1
        assert client.submits == ["1001"]
        assert "success" in event_names(events)

    def test_满员时持续等待直到有余量(self):
        client = RoundAdvancingClient([
            {"科幻文学": [("1001", 0, "张三")]},
            {"科幻文学": [("1001", 0, "张三")]},
            {"科幻文学": [("1001", 2, "张三")]},
        ])
        cfg = make_config([WatchTarget(name="科幻文学")])
        stats, events, _ = run_poller(client, cfg)

        assert stats.successes == 1
        assert client.submits == ["1001"]
        # 前两轮应该都是「未命中」，没有提交
        assert stats.rounds >= 3

    def test_全程满员则一直不提交(self):
        client = FakeClient([{"科幻文学": [("1001", 0, "张三")]}])
        cfg = make_config([WatchTarget(name="科幻文学")])
        stats, events, _ = run_poller(client, cfg, max_rounds=6)

        assert stats.successes == 0
        assert client.submits == []
        assert stats.rounds == 6
        assert "finish" in event_names(events)

    def test_一轮结束后报告统计(self):
        client = FakeClient([{"科幻文学": [("1001", 1, "张三")]}])
        cfg = make_config([WatchTarget(name="科幻文学")])
        stats, _, _ = run_poller(client, cfg)
        assert "轮次" in stats.summary()
        assert stats.elapsed >= 0


class TestPriority:
    def test_按优先级选择课程(self):
        """两门课都有余量时，优先级小的先提交。"""
        client = FakeClient([{
            "低优先级课": [("2002", 5, "李四")],
            "高优先级课": [("1001", 5, "张三")],
        }])
        cfg = make_config([
            WatchTarget(name="低优先级课", priority=200),
            WatchTarget(name="高优先级课", priority=10),
        ])
        _, _, _ = run_poller(client, cfg)
        assert client.submits[0] == "1001"

    def test_同优先级按教学班ID排序(self):
        client = FakeClient([{"课": [("3003", 5, ""), ("1001", 5, ""), ("2002", 5, "")]}])
        cfg = make_config([WatchTarget(name="课")])
        cfg.notify.stop_on_success = False  # 允许把三个班都试一遍，才能看出顺序
        _, _, _ = run_poller(client, cfg, max_rounds=1)
        assert client.submits == ["1001", "2002", "3003"]

    def test_禁用课程不参与(self):
        client = FakeClient([{
            "启用课": [("1001", 5, "")],
            "禁用课": [("2002", 5, "")],
        }])
        cfg = make_config([
            WatchTarget(name="启用课", enabled=True),
            WatchTarget(name="禁用课", enabled=False),
        ])
        _, _, _ = run_poller(client, cfg)
        assert client.submits == ["1001"]


class TestCapacityChanges:
    def test_从满员变有余量后被抢到(self):
        client = RoundAdvancingClient([
            {"课": [("1001", 0, "")]},
            {"课": [("1001", 1, "")]},
        ])
        cfg = make_config([WatchTarget(name="课")])
        stats, _, _ = run_poller(client, cfg)
        assert stats.successes == 1

    def test_多个教学班时选第一个有余量的(self):
        client = FakeClient([{"课": [("1001", 0, ""), ("1002", 3, "")]}])
        cfg = make_config([WatchTarget(name="课")])
        _, _, _ = run_poller(client, cfg)
        assert client.submits == ["1002"]

    def test_老师筛选只提交匹配的教学班(self):
        client = FakeClient([{"课": [("1001", 3, "张三"), ("1002", 3, "李四")]}])
        cfg = make_config([WatchTarget(name="课", teachers=["李四"])])
        _, _, _ = run_poller(client, cfg)
        assert client.submits == ["1002"]


class TestOutcomes:
    def test_已选过视为完成不再轮询(self):
        client = FakeClient(
            [{"课": [("1001", 5, "")]}],
            submit_results=[SelectionResult(outcome=SelectionOutcome.ALREADY, message="已选")],
        )
        cfg = make_config([WatchTarget(name="课")])
        stats, events, _ = run_poller(client, cfg)
        assert "already" in event_names(events)
        assert stats.successes == 0
        assert client.submits == ["1001"]

    def test_时间冲突后不再重复提交该班(self):
        """冲突不会因为等待而消失，所以必须标记为不可行，避免每轮空转。"""
        client = FakeClient(
            [{"课": [("1001", 5, "")]}],
            submit_results=[SelectionResult(outcome=SelectionOutcome.CONFLICT, message="冲突")],
        )
        cfg = make_config([WatchTarget(name="课")])
        cfg.notify.stop_on_success = False
        _, events, _ = run_poller(client, cfg, max_rounds=4, advance=False)

        assert "conflict" in event_names(events)
        # 只提交过一次 —— 后续轮次里该教学班已被标记为 CONFLICT，不再出现在候选里
        assert client.submits == ["1001"]
        assert client.queries > 1

    def test_满员结果继续轮询(self):
        client = RoundAdvancingClient(
            [{"课": [("1001", 5, "")]}] * 4,
            submit_results=[
                SelectionResult(outcome=SelectionOutcome.FULL, message="超过限选人数"),
                SelectionResult(outcome=SelectionOutcome.SUCCESS, message="选课成功"),
            ],
        )
        cfg = make_config([WatchTarget(name="课")])
        stats, events, _ = run_poller(client, cfg)
        assert "miss" in event_names(events)
        assert stats.successes == 1

    def test_提交时网络异常不崩溃(self):
        client = FakeClient(
            [{"课": [("1001", 5, "")]}],
            submit_results=[NetworkError("超时"), SelectionResult(outcome=SelectionOutcome.SUCCESS, message="成功")],
        )
        cfg = make_config([WatchTarget(name="课")])
        stats, events, _ = run_poller(client, cfg)
        assert "error" in event_names(events)
        assert stats.successes == 1


class TestAuthRecovery:
    def test_查询时登录失效触发重登(self):
        class ExpiringClient(FakeClient):
            def __init__(self):
                super().__init__([{"课": [("1001", 1, "")]}])
                self.expired_once = False

            def find_teaching_classes(self, keyword, **kwargs):
                if not self.expired_once:
                    self.expired_once = True
                    raise TokenExpired("token 失效")
                return super().find_teaching_classes(keyword, **kwargs)

        client = ExpiringClient()
        cfg = make_config([WatchTarget(name="课")])
        stats, events, _ = run_poller(client, cfg)

        assert stats.relogins == 1
        assert "relogin" in event_names(events)
        assert stats.successes == 1

    def test_提交时登录失效触发重登(self):
        client = RoundAdvancingClient(
            [{"课": [("1001", 5, "")]}] * 3,
            submit_results=[TokenExpired("失效"), SelectionResult(outcome=SelectionOutcome.SUCCESS, message="成功")],
        )
        cfg = make_config([WatchTarget(name="课")])
        stats, _, _ = run_poller(client, cfg)
        assert stats.relogins == 1

    def test_登录失败直接退出不重试(self):
        """账号密码错误反复重试会锁账号，必须立刻停。"""
        events: list[tuple[str, dict]] = []

        class FailingAuth:
            def login(self, username, password):
                raise LoginError("账号或密码错误")

        class FakeHttp:
            def set_token(self, t): pass
            @property
            def cookies(self): return {}
            @cookies.setter
            def cookies(self, v): pass

        cfg = make_config([WatchTarget(name="课")])
        poller = Poller(
            cfg, FailingAuth(), FakeClient([{"课": []}]), FakeHttp(),
            on_event=lambda e, p: events.append((e, p)),
            sleep=lambda s: None,
        )
        stats = poller.run()
        assert "fatal" in event_names(events)
        assert stats.rounds == 0


class TestRateLimit:
    def test_限流触发冷却并放大间隔(self):
        class LimitedClient(FakeClient):
            def __init__(self):
                super().__init__([{"课": [("1001", 1, "")]}])
                self.limited = False

            def find_teaching_classes(self, keyword, **kwargs):
                if not self.limited:
                    self.limited = True
                    raise RateLimited("请求过于频繁")
                return super().find_teaching_classes(keyword, **kwargs)

        client = LimitedClient()
        cfg = make_config([WatchTarget(name="课")])
        stats, events, _ = run_poller(client, cfg)

        assert stats.rate_limited == 1
        assert "rate_limited" in event_names(events)
        # 间隔被放大
        assert any(p.get("interval", 0) > cfg.poll.interval for e, p in events if e == "rate_limited")

    def test_连续错误超过阈值后停止(self):
        class BrokenClient(FakeClient):
            def find_teaching_classes(self, keyword, **kwargs):
                raise NetworkError("网络不通")

        cfg = make_config([WatchTarget(name="课")], max_consecutive_errors=3)
        stats, events, _ = run_poller(BrokenClient([{"课": []}]), cfg)

        assert "fatal" in event_names(events)
        assert stats.errors >= 3

    def test_成功后错误计数归零(self):
        class FlakyClient(FakeClient):
            def __init__(self):
                super().__init__([{"课": [("1001", 1, "")]}])
                self.n = 0

            def find_teaching_classes(self, keyword, **kwargs):
                self.n += 1
                if self.n == 1:
                    raise NetworkError("抖动")
                return super().find_teaching_classes(keyword, **kwargs)

        cfg = make_config([WatchTarget(name="课")], max_consecutive_errors=2)
        stats, _, _ = run_poller(FlakyClient(), cfg)
        # 抖动一次后恢复，不应该被判为致命错误
        assert stats.successes == 1


class TestDryRun:
    def test_dry_run_绝不提交选课(self):
        client = FakeClient([{"课": [("1001", 5, "")]}])
        cfg = make_config([WatchTarget(name="课")])
        stats, events, _ = run_poller(client, cfg, dry_run=True)

        assert client.submits == []
        assert stats.attempts == 0
        assert stats.successes == 0
        assert "dry_run_skip" in event_names(events)


class TestStop:
    def test_stop_立刻生效(self):
        client = FakeClient([{"课": [("1001", 0, "")]}])
        cfg = make_config([WatchTarget(name="课")])
        stop = threading.Event()
        stop.set()

        class FakeHttp:
            def set_token(self, t): pass
            @property
            def cookies(self): return {}
            @cookies.setter
            def cookies(self, v): pass

        poller = Poller(
            cfg, FakeAuth(), client, FakeHttp(), stop_event=stop, sleep=lambda s: None
        )
        stats = poller.run()
        assert stats.rounds == 0

    def test_所有课程完成后自动结束(self):
        client = FakeClient([{"课": [("1001", 5, "")]}])
        cfg = make_config([WatchTarget(name="课")])
        stats, events, _ = run_poller(client, cfg)
        assert "all_done" in event_names(events)


class TestBatchUnavailable:
    def test_不在批次内时立即退出(self):
        from bitxk.exceptions import NotInBatchError

        class NoBatchClient(FakeClient):
            def current_batch(self):
                raise NotInBatchError("当前不在可选课时间内")

        cfg = make_config([WatchTarget(name="课")])
        events: list[tuple[str, dict]] = []

        class FakeHttp:
            def set_token(self, t): pass
            @property
            def cookies(self): return {}
            @cookies.setter
            def cookies(self, v): pass

        poller = Poller(
            cfg, FakeAuth(), NoBatchClient([{"课": []}]), FakeHttp(),
            on_event=lambda e, p: events.append((e, p)),
            sleep=lambda s: None,
        )
        stats = poller.run()
        assert "fatal" in event_names(events)
        assert stats.rounds == 0
