"""图形界面测试。

tkinter 需要显示环境，所以在无头 CI 上整组跳过；本机跑时会构造真窗口、
渲染各类事件、验证配置保存回读。

原则：**不启动任何网络请求**，也不打开浏览器。界面的职责只是"把引擎事件
画出来"，所以这里全部用构造的假事件驱动。
"""

from __future__ import annotations

import tomllib

import pytest

from bitxk.client import CourseType
from bitxk.config import WatchTarget

tk = pytest.importorskip("tkinter", reason="没有 tkinter，跳过界面测试")


@pytest.fixture
def app(tmp_path):
    """构造一个真实窗口，并在测试结束后销毁。"""
    import tkinter as tk_mod

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
        assert values[1] == CourseType.label(CourseType.XGXK)

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


class TestCapacityRendering:
    def test_有余量的课用绿色标签(self, app):
        app._render_poller(
            "status",
            {
                "course": "科幻文学",
                "classes": [
                    {
                        "id": "1001",
                        "teacher": "张三",
                        "capacity": "12/40 (余 28)",
                        "status": "available",
                        "status_label": "有余量",
                    }
                ],
            },
        )
        item = app.cap_tree.get_children()[0]
        assert app.cap_tree.item(item, "tags") == ("available",)
        assert app.cap_tree.item(item, "values")[4] == "有余量"

    def test_已满的课用灰色标签(self, app):
        app._render_poller(
            "status",
            {
                "course": "体育/羽毛球",
                "classes": [
                    {
                        "id": "2001",
                        "teacher": "王五",
                        "capacity": "30/30 (余 0)",
                        "status": "full",
                        "status_label": "已满",
                    }
                ],
            },
        )
        item = app.cap_tree.get_children()[0]
        assert app.cap_tree.item(item, "tags") == ("full",)

    def test_冲突用黄色标签(self, app):
        app._render_poller(
            "status",
            {
                "course": "课",
                "classes": [{"id": "1", "status": "conflict", "status_label": "冲突"}],
            },
        )
        item = app.cap_tree.get_children()[0]
        assert app.cap_tree.item(item, "tags") == ("conflict",)

    def test_查不到课程时也显示一行(self, app):
        app._render_poller("status", {"course": "不存在的课", "classes": []})
        assert len(app.cap_tree.get_children()) == 1
        assert "未找到" in app.cap_tree.item(app.cap_tree.get_children()[0], "values")[4]

    def test_同一门课刷新时替换旧行而不是累加(self, app):
        """轮询会反复推送同一门课的状态，表格必须保持每门课最新一行。"""
        for capacity in ("余 5", "余 4", "余 3"):
            app._render_poller(
                "status",
                {
                    "course": "课",
                    "classes": [
                        {
                            "id": "1",
                            "capacity": capacity,
                            "status": "available",
                            "status_label": "有余量",
                        }
                    ],
                },
            )
        rows = app.cap_tree.get_children()
        assert len(rows) == 1
        assert app.cap_tree.item(rows[0], "values")[3] == "余 3"

    def test_多门课各自独立成行(self, app):
        for course in ("A", "B"):
            app._render_poller(
                "status",
                {
                    "course": course,
                    "classes": [{"id": "1", "status": "full", "status_label": "已满"}],
                },
            )
        assert len(app.cap_tree.get_children()) == 2

    def test_多个教学班各占一行(self, app):
        app._render_poller(
            "status",
            {
                "course": "课",
                "classes": [
                    {"id": "1", "status": "available", "status_label": "有余量"},
                    {"id": "2", "status": "full", "status_label": "已满"},
                ],
            },
        )
        assert len(app.cap_tree.get_children()) == 2


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
