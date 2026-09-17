"""图形界面测试。

tkinter 需要显示环境，所以在无头 CI 上整组跳过；本机跑时会构造真窗口、
渲染各类事件、验证配置保存回读。

原则：**不启动任何网络请求**，也不打开浏览器。界面的职责只是"把引擎事件
画出来"，所以这里全部用构造的假事件驱动。
"""

from __future__ import annotations

import os
import tomllib

import pytest

from bitxk.client import CourseType
from bitxk.config import WatchTarget

tk = pytest.importorskip("tkinter", reason="没有 tkinter，跳过界面测试")


@pytest.fixture
def app(tmp_path, monkeypatch):
    """构造一个真实窗口，并在测试结束后销毁。

    界面启动时会尝试恢复上次的登录态并**向服务端校验**，测试必须保持离线。
    这里把校验打成"无法判断"，顺带覆盖真实场景的一个要求：校园网不可达时
    登录态必须被保留，而不是被当成失效丢掉。
    """
    import tkinter as tk_mod

    from bitxk.auth import SessionCheck

    monkeypatch.setattr("bitxk.auth.check_session", lambda *a, **k: SessionCheck.UNKNOWN)
    monkeypatch.setattr("bitxk.gui.BitxkApp._restore_verify_worker", lambda self, cached: None)

    try:
        root = tk_mod.Tk()
    except tk_mod.TclError:
        pytest.skip("没有可用的显示环境")
    root.withdraw()  # 不弹到屏幕上打扰用户

    from bitxk.gui import BitxkApp

    application = BitxkApp(root, config_path=tmp_path / "config.toml")
    root.update()
    try:
        yield application
    finally:
        root.destroy()


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------


class TestConstruction:
    def test_窗口能构建(self, app):
        assert app.course_tree is not None
        assert app.cap_tree is not None
        assert app.log_text is not None

    def test_没有配置文件也能启动(self, app):
        """第一次使用的用户没有 config.toml，界面必须照样可用。"""
        assert app.cfg is not None
        assert app.cfg.courses == []
        assert "未加载配置文件" in app.log_text.get("1.0", "end")

    def test_启动时按钮状态正确(self, app):
        assert str(app.start_btn["state"]) == "normal"
        assert str(app.stop_btn["state"]) == "disabled"

    def test_标题与登录态提示(self, app):
        assert "未登录" in app.login_var.get()


# --------------------------------------------------------------------------
# 课程管理
# --------------------------------------------------------------------------


class TestCourseManagement:
    def _add(self, app, name="测试课", type_=CourseType.XGXK, priority=100):
        app.cfg.courses.append(WatchTarget(name=name, type=type_, priority=priority))
        app._refresh_course_tree()

    def test_添加课程后出现在列表(self, app):
        self._add(app)
        assert len(app.course_tree.get_children()) == 1
        values = app.course_tree.item(app.course_tree.get_children()[0], "values")
        assert values[0] == "测试课"

    def test_禁用的课有标记(self, app):
        app.cfg.courses.append(WatchTarget(name="停用课", enabled=False))
        app._refresh_course_tree()
        values = app.course_tree.item(app.course_tree.get_children()[0], "values")
        assert "已禁用" in values[0]

    def test_删除课程(self, app):
        self._add(app)
        app.course_tree.selection_set(app.course_tree.get_children()[0])
        app._on_delete_course()
        assert app.cfg.courses == []
        assert len(app.course_tree.get_children()) == 0

    def test_未选中时编辑与删除不崩(self, app):
        app._on_edit_course()
        app._on_delete_course()
        assert app.cfg.courses == []

    def test_多门课按添加顺序展示(self, app):
        self._add(app, "A")
        self._add(app, "B")
        self._add(app, "C")
        names = [app.course_tree.item(i, "values")[0] for i in app.course_tree.get_children()]
        assert names == ["A", "B", "C"]


# --------------------------------------------------------------------------
# 余量渲染
# --------------------------------------------------------------------------


class TestCourseTable:
    """课程查询表格：渲染、合并、筛选、排序（全部本地逻辑，无网络）。"""

    def _rows(self, app, rows):
        app._ingest_rows(rows)

    @staticmethod
    def _row(
        key,
        course="课",
        tc_type="XGXK",
        tc_class="1001",
        teacher="张",
        capacity=40,
        remaining=5,
        status="available",
        place="周一 1-2 节",
    ):
        return {
            "course": course,
            "type": tc_type,
            "class": tc_class,
            "teacher": teacher,
            "place": place,
            "time": "周一 1-2节",
            "capacity": capacity,
            "remaining": remaining,
            "selected": (capacity - remaining)
            if (capacity is not None and remaining is not None)
            else None,
            "status": status,
            "status_label": {
                "available": "有余量",
                "full": "已满",
                "conflict": "冲突",
                "selected": "已选",
            }.get(status, status),
            "capacity_text": f"{capacity - (remaining or 0)}/{capacity}",
            "key": key,
        }

    # ---------------- 渲染 ----------------

    def test_渲染到表格(self, app):
        self._rows(app, [self._row("XGXK:1", course="科幻文学")])
        items = app.cap_tree.get_children()
        assert len(items) == 1
        values = app.cap_tree.item(items[0], "values")
        # 精简后只有四列：课程 / 老师 / 上课时间 / 剩余
        assert values[0] == "科幻文学"
        assert values[1] == "张"  # 老师
        assert values[2] == "周一 1-2节"  # 上课时间
        assert str(values[3]) == "5"  # 剩余

    def test_表格只保留必要列(self, app):
        """界面只留「课程 / 老师 / 上课时间 / 剩余」四列。"""
        assert app.cap_tree["columns"] == ("course", "teacher", "time", "remaining")

    def test_有余量用绿色标签(self, app):
        self._rows(app, [self._row("k1", status="available")])
        item = app.cap_tree.get_children()[0]
        assert app.cap_tree.item(item, "tags") == ("available",)

    def test_已满用灰色标签(self, app):
        self._rows(app, [self._row("k1", status="full", remaining=0)])
        item = app.cap_tree.get_children()[0]
        assert app.cap_tree.item(item, "tags") == ("full",)

    def test_冲突用黄色标签(self, app):
        self._rows(app, [self._row("k1", status="conflict")])
        item = app.cap_tree.get_children()[0]
        assert app.cap_tree.item(item, "tags") == ("conflict",)

    def test_同一教学班重复查询只保留一行(self, app):
        for remaining in (5, 4, 3):
            self._rows(app, [self._row("XGXK:1001", remaining=remaining)])
        items = app.cap_tree.get_children()
        assert len(items) == 1, "同一教学班不该累加出多行"
        assert str(app.cap_tree.item(items[0], "values")[3]) == "3"

    def test_不同教学班各自成行(self, app):
        self._rows(
            app,
            [
                self._row("XGXK:1", tc_class="1"),
                self._row("XGXK:2", tc_class="2"),
            ],
        )
        assert len(app.cap_tree.get_children()) == 2

    def test_计数显示(self, app):
        self._rows(app, [self._row(f"k{i}") for i in range(3)])
        assert "3" in app.count_var.get()

    # ---------------- 筛选 ----------------

    def test_只看有余量(self, app):
        self._rows(
            app,
            [
                self._row("a", course="有余量的", status="available"),
                self._row("b", course="满的", status="full", remaining=0),
            ],
        )
        app.filter_available_var.set(True)
        app._apply_filters()
        names = [app.cap_tree.item(i, "values")[0] for i in app.cap_tree.get_children()]
        assert names == ["有余量的"]

    def test_只看已满(self, app):
        self._rows(
            app,
            [
                self._row("a", course="有余量的", status="available"),
                self._row("b", course="满的", status="full", remaining=0),
            ],
        )
        app.filter_full_var.set(True)
        app._apply_filters()
        names = [app.cap_tree.item(i, "values")[0] for i in app.cap_tree.get_children()]
        assert names == ["满的"]

    def test_只看冲突(self, app):
        self._rows(
            app,
            [
                self._row("a", course="正常的", status="available"),
                self._row("b", course="冲突的", status="conflict"),
            ],
        )
        app.filter_conflict_var.set(True)
        app._apply_filters()
        names = [app.cap_tree.item(i, "values")[0] for i in app.cap_tree.get_children()]
        assert names == ["冲突的"]

    def test_多个筛选是或的关系(self, app):
        self._rows(
            app,
            [
                self._row("a", course="A", status="available"),
                self._row("b", course="B", status="full", remaining=0),
                self._row("c", course="C", status="conflict"),
            ],
        )
        app.filter_available_var.set(True)
        app.filter_conflict_var.set(True)
        app._apply_filters()
        names = sorted(app.cap_tree.item(i, "values")[0] for i in app.cap_tree.get_children())
        assert names == ["A", "C"]

    def test_清除筛选恢复全部(self, app):
        self._rows(app, [self._row("a"), self._row("b")])
        app.filter_full_var.set(True)
        app._apply_filters()
        app._on_clear_filters()
        assert len(app.cap_tree.get_children()) == 2

    # ---------------- 搜索与类型筛选 ----------------

    def test_按课程名搜索(self, app):
        self._rows(
            app,
            [
                self._row("a", course="科幻文学"),
                self._row("b", course="经济学原理"),
            ],
        )
        app.search_var.set("科幻")
        assert [app.cap_tree.item(i, "values")[0] for i in app.cap_tree.get_children()] == [
            "科幻文学"
        ]

    def test_按教师搜索(self, app):
        self._rows(
            app,
            [
                self._row("a", course="A", teacher="张三"),
                self._row("b", course="B", teacher="李四"),
            ],
        )
        app.search_var.set("李四")
        assert [app.cap_tree.item(i, "values")[0] for i in app.cap_tree.get_children()] == ["B"]

    def test_按教学班号搜索(self, app):
        self._rows(
            app,
            [
                self._row("a", tc_class="202620271AECG003501"),
                self._row("b", tc_class="9999"),
            ],
        )
        app.search_var.set("003501")
        assert len(app.cap_tree.get_children()) == 1

    def test_按课程类型筛选(self, app):
        self._rows(
            app,
            [
                self._row("a", course="公选", tc_type="XGXK"),
                self._row("b", course="体育", tc_type="TYKC"),
            ],
        )
        app.type_var.set("TYKC 体育")
        app._apply_filters()
        assert [app.cap_tree.item(i, "values")[0] for i in app.cap_tree.get_children()] == ["体育"]

    def test_搜索是本地过滤不触发请求(self, app):
        """输入即筛选，绝不能每敲一个字就发一次请求。"""
        self._rows(app, [self._row("a", course="科幻文学")])
        before = list(app._rows)
        app.search_var.set("幻")
        assert app._rows == before, "搜索不该改动数据源"

    # ---------------- 排序 ----------------

    def test_按余量排序(self, app):
        self._rows(
            app,
            [
                self._row("a", course="少", remaining=1),
                self._row("b", course="多", remaining=30),
            ],
        )
        app._on_sort_column("remaining")
        got = [app.cap_tree.item(i, "values")[0] for i in app.cap_tree.get_children()]
        assert got == ["少", "多"]
        app._on_sort_column("remaining")  # 再点一次降序
        got = [app.cap_tree.item(i, "values")[0] for i in app.cap_tree.get_children()]
        assert got == ["多", "少"]

    def test_按课程名排序(self, app):
        self._rows(app, [self._row("a", course="B"), self._row("b", course="A")])
        app._on_sort_column("course")
        got = [app.cap_tree.item(i, "values")[0] for i in app.cap_tree.get_children()]
        assert got == ["A", "B"]

    def test_默认把有余量的排前面(self, app):
        self._rows(
            app,
            [
                self._row("a", course="满的", status="full", remaining=0),
                self._row("b", course="有余量", status="available", remaining=2),
            ],
        )
        got = [app.cap_tree.item(i, "values")[0] for i in app.cap_tree.get_children()]
        assert got[0] == "有余量", f"默认排序应把有余量的放前面：{got}"

    # ---------------- 加入要盯的课程 ----------------

    def test_双击行加入要盯的课程(self, app):
        self._rows(app, [self._row("XGXK:1001", course="科幻文学", tc_type="XGXK")])
        item = app.cap_tree.get_children()[0]
        app.cap_tree.selection_set(item)
        app._on_watch_selected_row()
        assert [t.name for t in app.cfg.courses] == ["科幻文学"]
        assert app.cfg.courses[0].type == "XGXK"

    def test_重复加入会被忽略(self, app):
        from bitxk.config import WatchTarget

        app.cfg.courses.append(WatchTarget(name="科幻文学"))
        self._rows(app, [self._row("XGXK:1001", course="科幻文学")])
        app.cap_tree.selection_set(app.cap_tree.get_children()[0])
        app._on_watch_selected_row()
        assert len(app.cfg.courses) == 1

    # ---------------- 轮询结果并入 ----------------

    def test_轮询结果并入已有行(self, app):
        self._rows(app, [self._row("XGXK:1001", tc_class="1001", remaining=5)])
        app._update_capacity_rows(
            {
                "course": "科幻文学",
                "classes": [
                    {
                        "id": "1001",
                        "teacher": "张",
                        "capacity": "12/40 (余 3)",
                        "remaining": 3,
                        "status": "available",
                        "status_label": "有余量",
                    }
                ],
            }
        )
        items = app.cap_tree.get_children()
        assert len(items) == 1, "轮询更新同一教学班不该新增行"
        assert str(app.cap_tree.item(items[0], "values")[3]) == "3"

    def test_轮询新教学班会被追加(self, app):
        app._update_capacity_rows(
            {
                "course": "新课",
                "classes": [
                    {
                        "id": "9999",
                        "capacity": "1/10 (余 9)",
                        "remaining": 9,
                        "status": "available",
                        "status_label": "有余量",
                    }
                ],
            }
        )
        assert len(app.cap_tree.get_children()) == 1


class TestWatchDetailPanel:
    """中间栏：所选任务的备选教学班（容量 / 已选 / 余量）。"""

    @staticmethod
    def _payload(course="金融学概论", rows=None):
        return {
            "course": course,
            "rows": rows
            if rows is not None
            else [
                {
                    "class": "1001",
                    "teacher": "马明",
                    "capacity": 120,
                    "selected": 121,
                    "remaining": 0,
                    "status": "full",
                    "status_label": "已满",
                    "place": "周三 3-4 节",
                },
                {
                    "class": "1002",
                    "teacher": "李四",
                    "capacity": 60,
                    "selected": 40,
                    "remaining": 20,
                    "status": "available",
                    "status_label": "有余量",
                    "place": "周五 1-2 节",
                },
            ],
        }

    def test_中间栏列精简(self, app):
        """中栏同样只留必要信息，外加教学班号用于区分同名的班。"""
        assert app.detail_tree["columns"] == ("class", "teacher", "time", "remaining")

    def test_渲染教学班与人数(self, app):
        app._detail_course = "金融学概论"
        app._render_detail(self._payload())
        items = app.detail_tree.get_children()
        assert len(items) == 2
        v = app.detail_tree.item(items[0], "values")
        # 列序：教学班 / 老师 / 上课时间 / 剩余
        assert v[0] == "1001"  # 教学班
        assert v[1] == "马明"  # 老师
        assert v[3] == "0"  # 剩余

    def test_标题汇总有余量班数(self, app):
        app._detail_course = "金融学概论"
        app._render_detail(self._payload())
        assert "2 个班" in app.detail_title_var.get()
        assert "1 个有余量" in app.detail_title_var.get()

    def test_全部已满时标题说明(self, app):
        app._detail_course = "课"
        app._render_detail(
            {
                "course": "课",
                "rows": [
                    {
                        "class": "1",
                        "capacity": 10,
                        "selected": 10,
                        "remaining": 0,
                        "status": "full",
                        "status_label": "已满",
                    },
                ],
            }
        )
        assert "均已满" in app.detail_title_var.get()

    def test_未找到教学班时标题说明(self, app):
        app._detail_course = "查无此课"
        app._render_detail({"course": "查无此课", "rows": []})
        assert "未找到" in app.detail_title_var.get()

    def test_丢弃过期结果(self, app):
        """用户已切到别的课，旧结果不该覆盖中间栏。"""
        app._detail_course = "新课"
        app._render_detail(self._payload(course="旧课"))
        assert len(app.detail_tree.get_children()) == 0

    def test_已满用灰色标签(self, app):
        app._detail_course = "课"
        app._render_detail(
            {
                "course": "课",
                "rows": [
                    {"class": "1", "status": "full", "status_label": "已满"},
                ],
            }
        )
        item = app.detail_tree.get_children()[0]
        assert app.detail_tree.item(item, "tags") == ("full",)

    def test_轮询事件同步刷新中间栏(self, app):
        """轮询的正是中间栏那门课时应同步更新，保持数据新鲜。"""
        app._detail_course = "金融学概论"
        app._render_detail(self._payload())
        app._render_poller(
            "status",
            {
                "course": "金融学概论",
                "classes": [
                    {
                        "id": "1001",
                        "teacher": "马明",
                        "capacity": "121/120 (余 0)",
                        "capacity_total": 120,
                        "selected": 121,
                        "remaining": 0,
                        "status": "full",
                        "status_label": "已满",
                        "place": "周三",
                    }
                ],
            },
        )
        items = app.detail_tree.get_children()
        assert len(items) == 1
        assert str(app.detail_tree.item(items[0], "values")[3]) == "0"

    def test_轮询别的课不动中间栏(self, app):
        app._detail_course = "金融学概论"
        app._render_detail(self._payload())
        before = len(app.detail_tree.get_children())
        app._render_poller(
            "status",
            {
                "course": "另一门课",
                "classes": [{"id": "9999", "status": "full", "status_label": "已满"}],
            },
        )
        assert len(app.detail_tree.get_children()) == before


# --------------------------------------------------------------------------
# 事件渲染
# --------------------------------------------------------------------------


class TestEventRendering:
    @pytest.mark.parametrize(
        ("event", "payload"),
        [
            ("student", {"name": "张三", "code": "1"}),
            ("batch", {"batch": "[B1] 第一轮"}),
            ("start", {"courses": 3}),
            ("attempt", {"course": "课", "class_id": "1"}),
            ("dry_run_skip", {"course": "课", "class_id": "1", "capacity": "余 3"}),
            ("success", {"course": "课", "class_id": "1"}),
            ("already", {"course": "课"}),
            ("pending", {"course": "课", "message": "处理中"}),
            ("conflict", {"course": "课", "class_id": "1"}),
            ("miss", {"course": "课", "outcome": "full", "message": "已满"}),
            ("miss", {"course": "课", "outcome": "error", "message": "其他"}),
            ("relogin", {}),
            ("rate_limited", {"cooldown": 30, "interval": 3}),
            ("server_busy", {"cooldown": 30, "message": "在线人数已满"}),
            ("warn", {"message": "警告"}),
            ("error", {"message": "错误"}),
            ("timeout", {"seconds": 100}),
            ("all_done", {}),
            ("interrupted", {}),
            ("fatal", {"message": "致命"}),
            ("finished", {"summary": "运行 1 分钟"}),
        ],
    )
    def test_所有事件都能渲染(self, app, event, payload):
        app._render_poller(event, payload)

    def test_未知事件不会崩(self, app):
        """引擎将来加了新事件，旧界面也不该炸。"""
        app._render_poller("某个未来才有的新事件", {"whatever": 1})

    def test_成功事件写入日志(self, app):
        app._render_poller("success", {"course": "科幻文学", "class_id": "1001"})
        assert "选课成功" in app.log_text.get("1.0", "end")

    def test_试跑提示不会说成成功(self, app):
        app._render_poller(
            "dry_run_skip",
            {
                "course": "课",
                "class_id": "1",
                "capacity": "余 3",
            },
        )
        text = app.log_text.get("1.0", "end")
        assert "试跑" in text
        assert "未提交" in text

    def test_batch_事件更新登录态显示(self, app):
        app._render_poller("student", {"name": "李四", "code": "1120200002"})
        assert "李四" in app.login_var.get()


# --------------------------------------------------------------------------
# 事件泵与状态机
# --------------------------------------------------------------------------


class TestEventPump:
    def _send(self, app, kind, payload):
        from bitxk.gui import _GuiEvent

        app._handle_event(_GuiEvent(kind, payload))

    def test_log_事件落到日志面板(self, app):
        before = int(app.log_text.index("end-1c").split(".")[0])
        self._send(app, "log", {"message": "来自线程的日志", "level": "ok"})
        assert "来自线程的日志" in app.log_text.get("1.0", "end")
        after = int(app.log_text.index("end-1c").split(".")[0])
        assert after == before + 1

    def test_login_ok_更新登录态(self, app):
        class FakeSession:
            cookies: dict = {}
            token = "T"
            student_code = "1"
            student_name = "张三"

        self._send(app, "login_ok", {"session": FakeSession(), "name": "张三", "code": "1"})
        assert "张三" in app.login_var.get()
        assert app.session is not None

    def test_login_ok_会把登录态写进磁盘(self, app, tmp_path):
        """图形界面此前从不保存登录态，导致每次打开都要重新登录。"""
        from bitxk.auth import Session

        session = Session(token="T", student_code="1120252751", student_name="彭煜涵")
        self._send(app, "login_ok", {"session": session, "name": "彭煜涵", "code": "1120252751"})

        saved = tmp_path / ".bitxk_session.json"
        assert saved.exists(), "登录成功后应当落盘"
        reloaded = Session.load(saved)
        assert reloaded is not None
        assert reloaded.token == "T"
        assert reloaded.student_code == "1120252751"

    def _restore_with_verdict(self, app, tmp_path, monkeypatch, verdict):
        """让"上次登录留下的"会话被恢复，并指定服务端校验的结论。

        复用 ``app`` fixture 已经建好的窗口 —— 同一个进程里再建第二个 Tk root
        会让 ``update()`` 卡死（实测），所以不能自己 new 一个。
        """
        from bitxk.auth import Session

        Session(token="T2", student_code="1120252751", student_name="彭煜涵").save(
            tmp_path / ".bitxk_session.json"
        )
        # _restore_session 会起后台线程做校验；测试里不必真的起线程，
        # 直接把结论交给渲染层，保持确定性。
        monkeypatch.setattr("bitxk.gui.BitxkApp._restore_verify_worker", lambda self, cached: None)
        app._restore_session()
        app._render_restore_session(verdict)

    def test_启动时恢复上次的登录态(self, app, tmp_path, monkeypatch):
        """重开程序应当自动恢复，而不是显示"未登录"。"""
        from bitxk.auth import SessionCheck

        self._restore_with_verdict(app, tmp_path, monkeypatch, SessionCheck.VALID.value)
        assert app.session is not None
        assert app.session.token == "T2"
        assert "彭煜涵" in app.login_var.get()
        log = app.log_text.get("1.0", "end")
        assert "已恢复上次的登录态" in log
        assert "仍然有效" in log

    def test_恢复的登录态失效时清掉(self, app, tmp_path, monkeypatch):
        from bitxk.auth import SessionCheck

        self._restore_with_verdict(app, tmp_path, monkeypatch, SessionCheck.EXPIRED.value)
        assert app.session is None
        assert "已失效" in app.login_var.get()
        assert "请点「用浏览器登录」重新登录" in app.log_text.get("1.0", "end")

    def test_网络不通时不丢登录态(self, app, tmp_path, monkeypatch):
        """实测踩过的坑：一次 SSL EOF 不该把好好的登录态扔掉。"""
        from bitxk.auth import SessionCheck

        self._restore_with_verdict(app, tmp_path, monkeypatch, SessionCheck.UNKNOWN.value)
        assert app.session is not None, "联网失败时不能丢弃登录态"
        assert app.session.token == "T2"
        assert "未能联网校验" in app.login_var.get()
        assert "登录信息已保留" in app.log_text.get("1.0", "end")

    def test_没有缓存会话时保持未登录(self, app):
        assert app.session is None
        assert "未登录" in app.login_var.get()

    def test_损坏的会话文件不影响启动(self, tmp_path):
        """会话文件坏掉只该被忽略，不能让界面起不来。"""
        import tkinter as tk_mod

        from bitxk.gui import BitxkApp

        (tmp_path / ".bitxk_session.json").write_bytes(b"\xff\xfe not utf8 at all")

        try:
            root = tk_mod.Tk()
        except tk_mod.TclError:
            pytest.skip("没有可用的显示环境")
        root.withdraw()
        application = BitxkApp(root, config_path=tmp_path / "config.toml")
        root.update()
        try:
            assert application.session is None
        finally:
            root.destroy()

    def test_verify_fail_把登录态标为失效(self, app):
        self._send(app, "verify_fail", {"message": "token 失效"})
        assert "失效" in app.login_var.get()

    def test_finished_报告统计(self, app):
        from bitxk.poller import PollStats

        self._send(
            app,
            "finished",
            {
                "stats": PollStats(rounds=5),
                "success": False,
                "started": True,
            },
        )
        assert "运行结束" in app.log_text.get("1.0", "end")

    def test_未启动成功会明确提示(self, app):
        from bitxk.poller import PollStats

        self._send(
            app,
            "finished",
            {
                "stats": PollStats(),
                "success": False,
                "started": False,
            },
        )
        assert "启动未完成" in app.log_text.get("1.0", "end")

    def test_idle_恢复按钮状态(self, app):
        app._set_busy(True)
        assert str(app.start_btn["state"]) == "disabled"
        self._send(app, "idle", {})
        assert str(app.start_btn["state"]) == "normal"
        assert str(app.stop_btn["state"]) == "disabled"

    def test_忙碌时按钮禁用(self, app):
        app._set_busy(True)
        assert str(app.start_btn["state"]) == "disabled"
        assert str(app.browser_btn["state"]) == "disabled"
        assert str(app.stop_btn["state"]) == "normal"


class TestConfigFromUi:
    def test_从界面读取间隔(self, app):
        app.interval_var.set("3.5")
        assert app._current_config().poll.interval == 3.5

    def test_非法间隔报错(self, app):
        from bitxk.exceptions import ConfigError

        app.interval_var.set("abc")
        with pytest.raises(ConfigError, match="必须是数字"):
            app._current_config()

    def test_复选框映射到配置(self, app):
        app.stop_on_success_var.set(True)
        assert app._current_config().notify.stop_on_success is True


# --------------------------------------------------------------------------
# 配置保存
# --------------------------------------------------------------------------


class TestSaveConfig:
    def test_保存后能回读(self, app, tmp_path):
        from bitxk.gui import save_config

        app.cfg.courses = [
            WatchTarget(name="科幻文学", type=CourseType.XGXK, priority=100),
            WatchTarget(
                name="体育/羽毛球",
                type=CourseType.TYKC,
                priority=50,
                teachers=["王"],
                enabled=False,
            ),
        ]
        out = tmp_path / "out.toml"
        save_config(app.cfg, out)

        data = tomllib.loads(out.read_text(encoding="utf-8"))
        assert len(data["courses"]) == 2
        assert data["courses"][0]["name"] == "科幻文学"
        assert data["courses"][1]["teachers"] == ["王"]
        assert data["courses"][1]["enabled"] is False

    def test_保存的文件能被_load_config_读回(self, app, tmp_path):
        """保存格式必须与读取端严格一致，否则用户改一次就坏一次。"""
        from bitxk.config import load_config
        from bitxk.gui import save_config

        app.cfg.courses = [WatchTarget(name="测试", type=CourseType.FANKC, priority=7)]
        out = tmp_path / "round-trip.toml"
        save_config(app.cfg, out)

        reloaded = load_config(out)
        assert len(reloaded.courses) == 1
        assert reloaded.courses[0].name == "测试"
        assert reloaded.courses[0].type == CourseType.FANKC
        assert reloaded.courses[0].priority == 7

    def test_特殊字符被正确转义(self, app, tmp_path):
        from bitxk.config import load_config
        from bitxk.gui import save_config

        tricky = '带"引号"和\\反斜杠'
        app.cfg.courses = [WatchTarget(name=tricky)]
        out = tmp_path / "escape.toml"
        save_config(app.cfg, out)

        assert load_config(out).courses[0].name == tricky

    @pytest.mark.skipif(os.name == "nt", reason="Windows 的 chmod 只支持只读位，没有 POSIX 权限")
    def test_配置文件权限收紧(self, app, tmp_path):
        from bitxk.gui import save_config

        out = tmp_path / "perm.toml"
        save_config(app.cfg, out)
        assert oct(out.stat().st_mode)[-3:] == "600"

    def test_保存已有配置不会丢字段(self, tmp_path):
        """用真实模板加载 → 保存 → 再读，确保没有字段被吞掉。"""
        from bitxk.config import SAMPLE_CONFIG, load_config
        from bitxk.gui import save_config

        src = tmp_path / "config.toml"
        src.write_text(SAMPLE_CONFIG, encoding="utf-8")
        cfg = load_config(src)

        out = tmp_path / "saved.toml"
        save_config(cfg, out)
        again = load_config(out)

        assert again.username == cfg.username
        assert again.poll.interval == cfg.poll.interval
        assert again.poll.min_request_interval == cfg.poll.min_request_interval
        assert again.notify.stop_on_success == cfg.notify.stop_on_success
        assert [c.name for c in again.courses] == [c.name for c in cfg.courses]


class TestRunGuiGuard:
    def test_无显示环境时给出可操作报错(self, monkeypatch):
        """SSH 里跑 gui 子命令时应提示改用命令行，而不是抛裸 TclError。"""
        import tkinter as tk_mod

        from bitxk.exceptions import BitxkError
        from bitxk.gui import run_gui

        def boom():
            raise tk_mod.TclError("no display name and no $DISPLAY environment variable")

        monkeypatch.setattr(tk_mod, "Tk", boom)
        with pytest.raises(BitxkError, match="bitxk grab"):
            run_gui()
