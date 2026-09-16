"""图形界面（tkinter 桌面窗口）。

设计取舍
--------
选 **tkinter + ttk** 而不是 Web 界面，理由是它零额外依赖：Python 自带，
双击就能开，不用起服务、不用占端口、也不怕用户误关浏览器标签。

但"抢课"本身是长时间后台任务，所以界面必须是**响应式**的：
所有网络请求都在工作线程里跑，通过 :class:`queue.Queue` 把事件送回主线程，
由 ``after()`` 轮询渲染。tkinter 的控件**只能在主线程碰**，这条纪律贯穿全文件。

窗口结构::

    ┌──────────────────────────────────────────────────────────┐
    │ [登录态] 未登录   [用浏览器登录]  [验证登录]              │
    ├────────────────────┬─────────────────────────────────────┤
    │ 要盯的课程          │ 实时余量                            │
    │ ┌────────────────┐ │ ┌─────────────────────────────────┐ │
    │ │ 科幻文学 公选  │ │ │ 课程  教学班  教师  容量  状态  │ │
    │ │ 体育/羽毛球    │ │ │ ...                             │ │
    │ └────────────────┘ │ └─────────────────────────────────┘ │
    │ [添加] [编辑] [删除]│                                     │
    ├────────────────────┴─────────────────────────────────────┤
    │ 间隔 [2.0]秒  [x]试跑  [开始抢课] [停止]                  │
    ├──────────────────────────────────────────────────────────┤
    │ 运行日志（滚动）                                          │
    └──────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

from .client import CourseType
from .config import Config, WatchTarget, load_config
from .exceptions import BitxkError, CaptchaRequired, ConfigError, LoginError, NotInBatchError
from .models import CourseStatus, SelectionOutcome

logger = logging.getLogger(__name__)

__all__ = ["run_gui"]


# --------------------------------------------------------------------------
# 配色（尽量贴近系统观感）
# --------------------------------------------------------------------------

COLORS = {
    "bg": "#f5f6f8",
    "panel": "#ffffff",
    "border": "#dcdfe6",
    "text": "#303133",
    "muted": "#909399",
    "primary": "#2f6fdb",
    "success": "#2ba471",
    "warning": "#e6a23c",
    "danger": "#e05c5c",
    "log_bg": "#1e2229",
    "log_text": "#d7dae0",
}


# --------------------------------------------------------------------------
# 课程编辑对话框
# --------------------------------------------------------------------------


class CourseDialog(tk.Toplevel):
    """新增 / 编辑一门要盯的课程。"""

    def __init__(self, parent: tk.Misc, target: WatchTarget | None = None) -> None:
        super().__init__(parent)
        self.title("编辑课程" if target else "添加课程")
        self.transient(parent)
        self.resizable(False, False)
        self.result: WatchTarget | None = None

        self.name_var = tk.StringVar(value=target.name if target else "")
        self.type_var = tk.StringVar(value=target.type if target else CourseType.XGXK)
        self.priority_var = tk.StringVar(value=str(target.priority if target else 100))
        self.teachers_var = tk.StringVar(value=",".join(target.teachers) if target else "")
        self.classes_var = tk.StringVar(value=",".join(target.classes) if target else "")
        self.enabled_var = tk.BooleanVar(value=target.enabled if target else True)

        body = ttk.Frame(self, padding=16)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)

        def row(index: int, label: str, widget: tk.Widget, hint: str = "") -> None:
            ttk.Label(body, text=label).grid(row=index, column=0, sticky="w", pady=6)
            widget.grid(row=index, column=1, sticky="ew", pady=6, padx=(10, 0))
            if hint:
                ttk.Label(body, text=hint, foreground=COLORS["muted"]).grid(
                    row=index, column=2, sticky="w", padx=(8, 0)
                )

        row(
            0,
            "课程名",
            ttk.Entry(body, textvariable=self.name_var, width=26),
            "须与选课系统显示完全一致",
        )
        row(
            1,
            "类型",
            ttk.Combobox(
                body,
                textvariable=self.type_var,
                state="readonly",
                width=24,
                values=[f"{code}" for code in CourseType.ALL],
            ),
            "XGXK 公选 / TYKC 体育 / FANKC 方案内…",
        )
        row(
            2, "优先级", ttk.Entry(body, textvariable=self.priority_var, width=26), "数字越小越先选"
        )
        row(
            3,
            "指定老师",
            ttk.Entry(body, textvariable=self.teachers_var, width=26),
            "可选，逗号分隔，支持部分匹配",
        )
        row(
            4,
            "指定教学班",
            ttk.Entry(body, textvariable=self.classes_var, width=26),
            "可选，留空则选到哪个算哪个",
        )

        ttk.Checkbutton(body, text="启用这门课", variable=self.enabled_var).grid(
            row=5, column=1, sticky="w", pady=(6, 0)
        )

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self._on_ok).pack(side="right", padx=(0, 8))

        self.bind("<Return>", lambda _e: self._on_ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._center(parent)
        self.grab_set()

    def _center(self, parent: tk.Misc) -> None:
        self.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")
        except tk.TclError:  # pragma: no cover
            pass

    def _on_ok(self) -> None:
        try:
            target = WatchTarget(
                name=self.name_var.get().strip(),
                type=self.type_var.get().strip(),
                priority=int(self.priority_var.get().strip() or 100),
                teachers=self.teachers_var.get().strip(),
                classes=self.classes_var.get().strip(),
                enabled=self.enabled_var.get(),
            )
        except ValueError:
            messagebox.showerror("优先级必须是数字", "请把优先级填成整数。", parent=self)
            return
        except ConfigError as exc:
            messagebox.showerror("配置有误", str(exc), parent=self)
            return
        self.result = target
        self.destroy()


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------


@dataclass
class _GuiEvent:
    """工作线程 → 主线程的事件。"""

    kind: str
    payload: dict


class BitxkApp(ttk.Frame):
    """主界面。"""

    def __init__(self, master: tk.Tk, *, config_path: str | Path | None = None) -> None:
        super().__init__(master, padding=0)
        self.master: tk.Tk = master
        self.config_path = Path(config_path) if config_path else Path.cwd() / "config.toml"

        self.cfg: Config | None = None
        self.events: queue.Queue[_GuiEvent] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.poller = None
        self.session = None
        self._stop_flag = threading.Event()
        self._busy = False
        #: 课程查询的独立停止信号与运行标记
        self._query_stop = threading.Event()
        self._query_running = False
        #: 当前批次 code（登录/轮询时缓存，查询课程时复用）
        self._batch_code = ""

        self._build_style()
        self._build_widgets()
        self._load_config_into_ui()
        self.after(80, self._drain_events)
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================================================================ 样式

    def _build_style(self) -> None:
        style = ttk.Style()
        # clam 在三大平台上表现一致，比默认主题好控制
        with contextlib.suppress(Exception):
            style.theme_use("clam")
        style.configure(".", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Panel.TFrame", background=COLORS["panel"], relief="flat")
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("Panel.TLabel", background=COLORS["panel"])
        style.configure("Muted.TLabel", foreground=COLORS["muted"])
        style.configure("Title.TLabel", font=("", 13, "bold"))
        style.configure(
            "Primary.TButton",
            font=("", 10, "bold"),
        )
        style.configure("TLabelframe", background=COLORS["bg"])
        style.configure("TLabelframe.Label", background=COLORS["bg"])
        style.configure(
            "Treeview",
            background=COLORS["panel"],
            fieldbackground=COLORS["panel"],
            rowheight=24,
        )
        style.configure("Treeview.Heading", font=("", 10, "bold"))

    # ================================================================ 布局

    def _build_widgets(self) -> None:
        self.grid(row=0, column=0, sticky="nsew")
        self.master.rowconfigure(0, weight=1)
        self.master.columnconfigure(0, weight=1)
        # 三栏：任务列表 | 该任务的备选课程 | 全部课程查询
        self.columnconfigure(0, weight=3, minsize=280)
        self.columnconfigure(1, weight=4, minsize=320)
        self.columnconfigure(2, weight=6, minsize=420)
        self.rowconfigure(1, weight=1)
        self.rowconfigure(3, weight=2)

        self._build_header()
        self._build_course_panel()
        self._build_watch_detail_panel()
        self._build_capacity_panel()
        self._build_control_bar()
        self._build_log_panel()

    # ---------------- 顶部：登录态 ----------------

    def _build_header(self) -> None:
        bar = ttk.Frame(self, padding=(14, 10))
        bar.grid(row=0, column=0, columnspan=3, sticky="ew")
        bar.columnconfigure(1, weight=1)

        ttk.Label(bar, text="BIT 选课助手", style="Title.TLabel").grid(row=0, column=0, sticky="w")

        self.login_var = tk.StringVar(value="登录态：未登录")
        self.login_label = ttk.Label(bar, textvariable=self.login_var, style="Muted.TLabel")
        self.login_label.grid(row=0, column=1, sticky="w", padx=(16, 0))

        self.browser_btn = ttk.Button(bar, text="用浏览器登录", command=self._on_browser_login)
        self.browser_btn.grid(row=0, column=2, padx=(0, 8))
        ttk.Button(bar, text="验证登录", command=self._on_verify_login).grid(
            row=0, column=3, padx=(0, 8)
        )
        ttk.Button(bar, text="自检", command=self._on_selfcheck).grid(row=0, column=4)

    # ---------------- 左侧：课程列表 ----------------

    def _build_course_panel(self) -> None:
        box = ttk.LabelFrame(self, text=" 要盯的课程 ", padding=10)
        box.grid(row=1, column=0, sticky="nsew", padx=(14, 6), pady=(0, 7))
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)

        columns = ("name", "type", "priority")
        self.course_tree = ttk.Treeview(box, columns=columns, show="headings", height=8)
        self.course_tree.heading("name", text="课程名")
        self.course_tree.heading("type", text="类型")
        self.course_tree.heading("priority", text="优先级")
        self.course_tree.column("name", width=170)
        self.course_tree.column("type", width=90, anchor="center")
        self.course_tree.column("priority", width=60, anchor="center")
        self.course_tree.grid(row=0, column=0, sticky="nsew")
        self.course_tree.bind("<Double-1>", lambda _e: self._on_edit_course())
        # 选中一门课 → 中间栏显示它的备选教学班
        self.course_tree.bind("<<TreeviewSelect>>", self._on_watch_selected)

        scroll = ttk.Scrollbar(box, orient="vertical", command=self.course_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.course_tree.configure(yscrollcommand=scroll.set)

        buttons = ttk.Frame(box)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="添加", command=self._on_add_course).pack(side="left")
        ttk.Button(buttons, text="编辑", command=self._on_edit_course).pack(side="left", padx=6)
        ttk.Button(buttons, text="删除", command=self._on_delete_course).pack(side="left")
        ttk.Button(buttons, text="保存配置", command=self._on_save_config).pack(side="right")

    # ---------------- 中间：所选任务的备选课程详情 ----------------

    def _build_watch_detail_panel(self) -> None:
        """显示「左侧选中的那门课」有哪些教学班，以及各自容量/已选人数。

        与右侧的区别：右侧是**全部备选课程**的广撒网浏览；这一栏是
        **我在盯的这门课**到底有几个班、每个班多少人、还差多少 ——
        决定「值不值得等」看的就是这里。
        """
        box = ttk.LabelFrame(self, text=" 该任务的备选教学班 ", padding=10)
        box.grid(row=1, column=1, sticky="nsew", padx=6, pady=(0, 7))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(1, weight=1)

        head = ttk.Frame(box)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.detail_title_var = tk.StringVar(value="（请先在左侧选一门课）")
        ttk.Label(head, textvariable=self.detail_title_var, style="Title.TLabel").pack(side="left")
        self.detail_refresh_btn = ttk.Button(head, text="刷新", command=self._on_refresh_detail)
        self.detail_refresh_btn.pack(side="right")

        columns = ("class", "teacher", "capacity", "selected", "remaining", "status", "place")
        self.detail_tree = ttk.Treeview(box, columns=columns, show="headings", height=8)
        specs = (
            ("class", "教学班", 130, "center"),
            ("teacher", "教师", 70, "w"),
            ("capacity", "容量", 60, "center"),
            ("selected", "已选", 60, "center"),
            ("remaining", "余量", 55, "center"),
            ("status", "状态", 70, "center"),
            ("place", "时间地点", 150, "w"),
        )
        for key, text, width, anchor_x in specs:
            self.detail_tree.heading(key, text=text)
            self.detail_tree.column(key, width=width, anchor=anchor_x)
        self.detail_tree.grid(row=1, column=0, sticky="nsew")

        vscroll = ttk.Scrollbar(box, orient="vertical", command=self.detail_tree.yview)
        vscroll.grid(row=1, column=1, sticky="ns")
        self.detail_tree.configure(yscrollcommand=vscroll.set)
        hscroll = ttk.Scrollbar(box, orient="horizontal", command=self.detail_tree.xview)
        hscroll.grid(row=2, column=0, sticky="ew")
        self.detail_tree.configure(xscrollcommand=hscroll.set)

        self.detail_tree.tag_configure("available", foreground=COLORS["success"])
        self.detail_tree.tag_configure("full", foreground=COLORS["muted"])
        self.detail_tree.tag_configure("selected", foreground=COLORS["primary"])
        self.detail_tree.tag_configure("conflict", foreground=COLORS["warning"])

        #: 中间栏当前显示的课程名（用于轮询时同步刷新）
        self._detail_course: str = ""
        self._detail_running = False

    # ---------------- 右侧：课程查询与筛选 ----------------

    def _build_capacity_panel(self) -> None:
        """课程浏览器：可查询全部备选课程，并按时段/容量/冲突筛选。

        设计要点：

        * 表格列可点标题排序（课程名 / 教师 / 余量 / 状态）；
        * 筛选与搜索都在**本地**做，不重新请求 —— 8 个课程类型全查一遍
          是 8 次请求，不能每敲一个字就重发；
        * 冲突与已满各自独立勾选，可以「只看有余量」也可以「只看冲突」。
        """
        box = ttk.LabelFrame(self, text=" 课程查询（全部备选） ", padding=10)
        box.grid(row=1, column=2, sticky="nsew", padx=(6, 14), pady=(0, 7))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(2, weight=1)

        # ---- 第一行：关键词 + 课程类型 + 查询按钮 ----
        row1 = ttk.Frame(box)
        row1.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        ttk.Label(row1, text="关键词").pack(side="left")
        self.search_var = tk.StringVar()
        search = ttk.Entry(row1, textvariable=self.search_var, width=18)
        search.pack(side="left", padx=(6, 10))
        # 输入即筛选（本地过滤，无网络请求）
        self.search_var.trace_add("write", lambda *_: self._apply_filters())

        ttk.Label(row1, text="类型").pack(side="left")
        self.type_var = tk.StringVar(value="全部")
        type_box = ttk.Combobox(
            row1,
            textvariable=self.type_var,
            state="readonly",
            width=12,
            values=["全部"] + [f"{c} {CourseType.label(c)}" for c in CourseType.ALL],
        )
        type_box.pack(side="left", padx=(6, 10))
        type_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_filters())

        self.query_all_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="查全部类型", variable=self.query_all_var).pack(
            side="left", padx=(0, 10)
        )

        self.query_btn = ttk.Button(row1, text="查询", command=self._on_query_courses)
        self.query_btn.pack(side="left")
        self.stop_query_btn = ttk.Button(
            row1, text="停止查询", command=self._on_stop_query, state="disabled"
        )
        self.stop_query_btn.pack(side="left", padx=(6, 0))

        # ---- 第二行：筛选复选框 ----
        row2 = ttk.Frame(box)
        row2.grid(row=1, column=0, sticky="ew", pady=(0, 6))

        ttk.Label(row2, text="筛选").pack(side="left")
        self.filter_available_var = tk.BooleanVar(value=False)
        self.filter_full_var = tk.BooleanVar(value=False)
        self.filter_conflict_var = tk.BooleanVar(value=False)
        self.filter_selected_var = tk.BooleanVar(value=False)
        for text, var in (
            ("只看有余量", self.filter_available_var),
            ("只看已满", self.filter_full_var),
            ("只看冲突", self.filter_conflict_var),
            ("只看已选", self.filter_selected_var),
        ):
            ttk.Checkbutton(row2, text=text, variable=var, command=self._apply_filters).pack(
                side="left", padx=(8, 0)
            )

        ttk.Button(row2, text="清除筛选", command=self._on_clear_filters).pack(
            side="left", padx=(14, 0)
        )

        self.count_var = tk.StringVar(value="尚未查询")
        ttk.Label(row2, textvariable=self.count_var, style="Muted.TLabel").pack(side="right")

        # ---- 表格 ----
        columns = (
            "course",
            "type",
            "class",
            "teacher",
            "place",
            "capacity",
            "remaining",
            "status",
            "updated",
        )
        self.cap_tree = ttk.Treeview(box, columns=columns, show="headings", height=8)
        specs = (
            ("course", "课程", 170, "w"),
            ("type", "类型", 80, "center"),
            ("class", "教学班", 150, "center"),
            ("teacher", "教师", 80, "w"),
            ("place", "时间地点", 170, "w"),
            ("capacity", "容量", 90, "center"),
            ("remaining", "余量", 60, "center"),
            ("status", "状态", 80, "center"),
            ("updated", "更新于", 70, "center"),
        )
        for key, text, width, anchor in specs:
            # 点列标题排序
            self.cap_tree.heading(key, text=text, command=lambda k=key: self._on_sort_column(k))
            self.cap_tree.column(key, width=width, anchor=anchor)
        self.cap_tree.grid(row=2, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(box, orient="vertical", command=self.cap_tree.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.cap_tree.configure(yscrollcommand=scroll.set)
        # 横向滚动（列多，窄窗口下需要）
        hscroll = ttk.Scrollbar(box, orient="horizontal", command=self.cap_tree.xview)
        hscroll.grid(row=3, column=0, sticky="ew")
        self.cap_tree.configure(xscrollcommand=hscroll.set)

        self.cap_tree.tag_configure("available", foreground=COLORS["success"])
        self.cap_tree.tag_configure("full", foreground=COLORS["muted"])
        self.cap_tree.tag_configure("selected", foreground=COLORS["primary"])
        self.cap_tree.tag_configure("conflict", foreground=COLORS["warning"])

        # 双击某一行把它加进「要盯的课程」
        self.cap_tree.bind("<Double-1>", self._on_watch_selected_row)

        # 内部数据：每行一个 dict，筛选/排序都基于它
        self._rows: list[dict] = []
        self._sort_key: str | None = None
        self._sort_desc = False

    # ---------------- 控制条 ----------------

    def _build_control_bar(self) -> None:
        bar = ttk.Frame(self, padding=(14, 4))
        bar.grid(row=2, column=0, columnspan=3, sticky="ew")

        ttk.Label(bar, text="轮询间隔").pack(side="left")
        self.interval_var = tk.StringVar(value="2.0")
        ttk.Entry(bar, textvariable=self.interval_var, width=6).pack(side="left", padx=(6, 2))
        ttk.Label(bar, text="秒").pack(side="left")

        self.dry_run_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="试跑（只观察不提交）", variable=self.dry_run_var).pack(
            side="left", padx=(18, 0)
        )

        self.stop_on_success_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="抢到一门就停", variable=self.stop_on_success_var).pack(
            side="left", padx=(12, 0)
        )

        self.stop_btn = ttk.Button(bar, text="停止", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.start_btn = ttk.Button(
            bar, text="开始抢课", style="Primary.TButton", command=self._on_start
        )
        self.start_btn.pack(side="right", padx=(0, 8))

    # ---------------- 底部：日志 ----------------

    def _build_log_panel(self) -> None:
        box = ttk.LabelFrame(self, text=" 运行日志 ", padding=8)
        box.grid(row=3, column=0, columnspan=3, sticky="nsew", padx=14, pady=(0, 12))
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)

        self.log_text = tk.Text(
            box,
            height=9,
            wrap="none",
            state="disabled",
            background=COLORS["log_bg"],
            foreground=COLORS["log_text"],
            insertbackground=COLORS["log_text"],
            relief="flat",
            font=("Menlo", 11) if _has_font("Menlo") else ("Courier", 11),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

        self.log_text.tag_configure("ok", foreground="#5dd39e")
        self.log_text.tag_configure("warn", foreground="#f0c674")
        self.log_text.tag_configure("err", foreground="#ff8a80")
        self.log_text.tag_configure("info", foreground=COLORS["log_text"])
        self.log_text.tag_configure("muted", foreground="#7d8590")

    # ================================================================ 日志

    def log(self, message: str, level: str = "info") -> None:
        """往日志面板追加一行（只在主线程调用）。"""
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp} ", "muted")
        self.log_text.insert("end", f"{message}\n", level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _log_threadsafe(self, message: str, level: str = "info") -> None:
        """供工作线程调用：把日志丢进队列。"""
        self.events.put(_GuiEvent("log", {"message": message, "level": level}))

    # ================================================================ 配置

    def _load_config_into_ui(self) -> None:
        try:
            self.cfg = load_config(self.config_path)
        except ConfigError as exc:
            # 没有配置文件不是错误，界面照常可用，让用户自己加课
            self.cfg = Config(base_dir=self.config_path.parent)
            self.log(f"未加载配置文件（{exc}）", "muted")
            self.log("可以直接在左侧添加课程，然后点「保存配置」。", "muted")
        else:
            self.log(f"已加载配置：{self.config_path}", "ok")
            self.interval_var.set(str(self.cfg.poll.interval))
            self.stop_on_success_var.set(self.cfg.notify.stop_on_success)
            if self.cfg.username:
                self.log(f"配置里的学号：{self.cfg.username}", "muted")

        self._refresh_course_tree()

    def _refresh_course_tree(self) -> None:
        self.course_tree.delete(*self.course_tree.get_children())
        if not self.cfg:
            return
        for target in self.cfg.courses:
            label = "" if target.enabled else "（已禁用）"
            self.course_tree.insert(
                "",
                "end",
                values=(target.name + label, CourseType.label(target.type), target.priority),
            )

    def _selected_course_index(self) -> int | None:
        selection = self.course_tree.selection()
        if not selection or not self.cfg:
            return None
        return self.course_tree.index(selection[0])

    def _on_add_course(self) -> None:
        dialog = CourseDialog(self.master)
        self.wait_window(dialog)
        if dialog.result and self.cfg:
            self.cfg.courses.append(dialog.result)
            self._refresh_course_tree()
            self.log(f"已添加课程：{dialog.result.name}", "ok")

    def _on_edit_course(self) -> None:
        index = self._selected_course_index()
        if index is None or not self.cfg:
            return
        dialog = CourseDialog(self.master, self.cfg.courses[index])
        self.wait_window(dialog)
        if dialog.result:
            self.cfg.courses[index] = dialog.result
            self._refresh_course_tree()
            self.log(f"已更新课程：{dialog.result.name}", "ok")

    def _on_delete_course(self) -> None:
        index = self._selected_course_index()
        if index is None or not self.cfg:
            return
        target = self.cfg.courses.pop(index)
        self._refresh_course_tree()
        self.log(f"已删除课程：{target.name}", "warn")

    def _on_save_config(self) -> None:
        if not self.cfg or not self.cfg.courses:
            messagebox.showinfo("没有课程", "请先添加至少一门课程。", parent=self.master)
            return
        try:
            save_config(self.cfg, self.config_path)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.master)
            return
        self.log(f"已保存到 {self.config_path}", "ok")
        messagebox.showinfo("已保存", f"配置已写入\n{self.config_path}", parent=self.master)

    # ================================================================ 浏览器登录

    def _on_browser_login(self) -> None:
        if self._busy:
            return
        self._set_busy(True, "正在打开浏览器…")
        self.log("正在检测本机浏览器…")
        threading.Thread(target=self._browser_login_worker, daemon=True).start()

    def _browser_login_worker(self) -> None:
        try:
            from .browser import browser_login, pick_browser

            browser = pick_browser()
            self._log_threadsafe(f"使用浏览器：{browser.name}", "muted")
            self._log_threadsafe(
                "已打开浏览器窗口，请在里面完成统一身份认证登录（最多等 5 分钟）…", "info"
            )

            from .auth import API_BASE, CAS_SERVICE_URL

            result = browser_login(
                api_base=API_BASE,
                service_url=CAS_SERVICE_URL,
                student_code=self.cfg.username if self.cfg else "",
                on_progress=lambda msg: self._log_threadsafe(msg, "muted"),
            )
            self.events.put(
                _GuiEvent(
                    "login_ok",
                    {
                        "session": result.session,
                        "name": result.student_name,
                        "code": result.student_code,
                    },
                )
            )
        except BitxkError as exc:
            self.events.put(_GuiEvent("login_fail", {"message": str(exc)}))
        except Exception as exc:  # pragma: no cover - 兜底
            logger.exception("浏览器登录异常")
            self.events.put(_GuiEvent("login_fail", {"message": f"意外错误：{exc}"}))

    def _on_verify_login(self) -> None:
        if self._busy:
            return
        if self.session is None:
            messagebox.showinfo(
                "还没有登录态",
                "请先点「用浏览器登录」，或在命令行用 --cookie/--token 导入。",
                parent=self.master,
            )
            return
        self._set_busy(True, "正在验证…")
        cfg = self._snapshot()
        threading.Thread(target=self._verify_worker, args=(cfg,), daemon=True).start()

    def _verify_worker(self, cfg: Config) -> None:
        try:
            from .cli import _build_http
            from .client import XkClient

            http = _build_http(cfg)
            try:
                http.cookies = dict(self.session.cookies)
                http.set_token(self.session.token)
                http.student_code = self.session.student_code
                client = XkClient(http)
                info = client.student_info(self.session.student_code)
                name = info.get("name") or self.session.student_name
                batch = client.current_batch(self.session.student_code)
                self.events.put(
                    _GuiEvent(
                        "verify_ok",
                        {
                            "name": str(name),
                            "batch": str(batch),
                        },
                    )
                )
            finally:
                http.close()
        except NotInBatchError as exc:
            self.events.put(_GuiEvent("verify_warn", {"message": str(exc)}))
        except BitxkError as exc:
            self.events.put(_GuiEvent("verify_fail", {"message": str(exc)}))
        except Exception as exc:  # pragma: no cover
            self.events.put(_GuiEvent("verify_fail", {"message": str(exc)}))

    # ================================================================ 自检

    def _on_selfcheck(self) -> None:
        if self._busy:
            return
        self._set_busy(True, "正在自检…")
        cfg = self._snapshot()
        threading.Thread(target=self._selfcheck_worker, args=(cfg,), daemon=True).start()

    def _selfcheck_worker(self, cfg: Config) -> None:
        from .auth import API_BASE, CAS_LOGIN_URL
        from .cli import _build_http

        try:
            http = _build_http(cfg)
            try:
                resp = http.get(f"{API_BASE}/*default/index.do")
                self._log_threadsafe(
                    f"选课系统：HTTP {resp.status_code}", "ok" if resp.status_code == 200 else "err"
                )
                resp = http.get(f"{API_BASE}/bitXsxkLogin/casLogin.do", allow_redirects=False)
                if "sso.bit.edu.cn" in resp.headers.get("Location", ""):
                    self._log_threadsafe("CAS 入口：正常（302 → 统一身份认证）", "ok")
                else:
                    self._log_threadsafe(f"CAS 入口：HTTP {resp.status_code}，重定向异常", "warn")
                resp = http.get(f"{CAS_LOGIN_URL}?service=x")
                if "login-croypto" in resp.text:
                    self._log_threadsafe("登录页结构：正常", "ok")
                else:
                    self._log_threadsafe("登录页结构已变更（不影响浏览器登录方式）", "warn")
            finally:
                http.close()
            self._log_threadsafe("自检完成", "ok")
        except Exception as exc:
            self._log_threadsafe(f"自检失败：{exc}", "err")
        finally:
            self.events.put(_GuiEvent("idle", {}))

    # ================================================================ 抢课

    def _snapshot(self) -> Config:
        """在主线程里把界面状态固化成一份配置快照，交给工作线程用。

        **线程纪律**：tkinter 变量（``StringVar.get()`` 等）只能在主线程读，
        在工作线程里读会抛 ``RuntimeError: main thread is not in main loop``
        （实测踩过）。所以所有 ``*_worker`` 都必须接收主线程预先建好的快照，
        绝不能在worker 里自己读界面。
        """
        return self._current_config()

    def _current_config(self) -> Config:
        """按界面上的当前状态生成配置（不落盘）。**只允许在主线程调用。**"""
        cfg = self.cfg or Config(base_dir=self.config_path.parent)
        try:
            cfg.poll.interval = float(self.interval_var.get())
        except ValueError:
            raise ConfigError("轮询间隔必须是数字") from None
        cfg.notify.stop_on_success = self.stop_on_success_var.get()
        cfg.notify.sound = True
        return cfg

    def _on_start(self) -> None:
        if self._busy:
            return
        if self.session is None:
            if not messagebox.askyesno(
                "还没有登录态",
                "还没有可用的登录态。\n\n现在打开浏览器登录一次吗？\n"
                "（选「否」将退回配置文件里的账号密码方式）",
                parent=self.master,
            ):
                self.log("未登录，无法开始。请先「用浏览器登录」。", "err")
                return
            self._on_browser_login()
            return

        try:
            cfg = self._current_config()
        except ConfigError as exc:
            messagebox.showerror("配置有误", str(exc), parent=self.master)
            return

        if not cfg.enabled_courses:
            messagebox.showinfo("没有课程", "请先在左侧添加至少一门课程。", parent=self.master)
            return

        dry_run = self.dry_run_var.get()
        self._stop_flag.clear()
        self._set_busy(True, "运行中…")
        # 只清掉轮询产生的行，保留查询出来的备选列表
        self._rows = [r for r in self._rows if not r["key"].startswith("poll:")]
        self.log(
            f"开始轮询 {len(cfg.enabled_courses)} 门课程"
            + ("（试跑模式，不会提交选课）" if dry_run else "（会自动提交选课）"),
            "ok",
        )
        self.worker = threading.Thread(target=self._grab_worker, args=(cfg, dry_run), daemon=True)
        self.worker.start()

    def _grab_worker(self, cfg: Config, dry_run: bool) -> None:
        from .auth import BitAuth
        from .cli import _build_http
        from .client import XkClient
        from .poller import Poller

        http = None
        try:
            http = _build_http(cfg)
            http.cookies = dict(self.session.cookies)
            http.set_token(self.session.token)
            http.student_code = self.session.student_code
            client = XkClient(http)

            def on_success(course: str, detail: str = "") -> None:
                self.events.put(_GuiEvent("notify", {"course": course}))

            # 注意：包装后的 handler 必须直接交给 Poller，
            # 不能在构造之后再改 poller.on_event —— 那样会导致每个事件被投递两次。
            poller = Poller(
                cfg,
                BitAuth(http),
                client,
                http,
                session=self.session,
                on_event=_wrap_poller_events(self._on_poller_event, on_success),
                stop_event=self._stop_flag,
                dry_run=dry_run,
            )
            self.poller = poller

            stats = poller.run()
            self.events.put(
                _GuiEvent(
                    "finished",
                    {
                        "stats": stats,
                        "success": stats.successes > 0,
                        "started": poller.started,
                    },
                )
            )
        except LoginError as exc:
            self.events.put(_GuiEvent("fatal", {"message": f"登录失败：{exc}"}))
        except NotInBatchError as exc:
            self.events.put(_GuiEvent("fatal", {"message": str(exc)}))
        except CaptchaRequired as exc:
            self.events.put(_GuiEvent("fatal", {"message": str(exc)}))
        except BitxkError as exc:
            self.events.put(_GuiEvent("fatal", {"message": str(exc)}))
        except Exception as exc:  # pragma: no cover
            logger.exception("抢课线程异常")
            self.events.put(_GuiEvent("fatal", {"message": f"意外错误：{exc}"}))
        finally:
            if http is not None:
                http.close()
            self.events.put(_GuiEvent("idle", {}))

    def _on_poller_event(self, event: str, payload: dict) -> None:
        """轮询引擎回调（工作线程）→ 转成 GUI 事件。"""
        self.events.put(_GuiEvent("poller", {"event": event, "payload": payload}))

    def _on_stop(self) -> None:
        if self.poller is not None:
            self.poller.stop()
        self._stop_flag.set()
        self.log("已请求停止，正在收尾…", "warn")
        self.stop_btn.configure(state="disabled")

    # ================================================================ 事件泵

    def _drain_events(self) -> None:
        """主线程侧的事件循环（tkinter 控件只能在这里被碰）。"""
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.after(80, self._drain_events)

    def _handle_event(self, event: _GuiEvent) -> None:
        kind, payload = event.kind, event.payload

        if kind == "log":
            self.log(payload["message"], payload.get("level", "info"))
        elif kind == "login_ok":
            self.session = payload["session"]
            name = payload.get("name") or payload.get("code") or "已登录"
            self.login_var.set(f"登录态：{name}")
            self.login_label.configure(foreground=COLORS["success"])
            self.log(f"登录成功：{name}", "ok")
            self._set_busy(False)
        elif kind == "login_fail":
            self.log(f"浏览器登录失败：{payload['message']}", "err")
            self._set_busy(False)
            messagebox.showerror("登录失败", payload["message"], parent=self.master)
        elif kind == "verify_ok":
            self.login_var.set(f"登录态：{payload['name']}（{payload['batch']}）")
            self.login_label.configure(foreground=COLORS["success"])
            self.log(f"登录态有效：{payload['name']} | {payload['batch']}", "ok")
            self._set_busy(False)
        elif kind == "verify_warn":
            self.log(f"登录态有效，但批次不可用：{payload['message']}", "warn")
            self._set_busy(False)
        elif kind == "verify_fail":
            self.login_var.set("登录态：已失效")
            self.login_label.configure(foreground=COLORS["danger"])
            self.log(f"登录态失效：{payload['message']}", "err")
            self._set_busy(False)
        elif kind == "detail_rows":
            self._render_detail(payload)
        elif kind == "detail_failed":
            self.log(f"「{payload['course']}」查询失败：{payload['message']}", "warn")
            self.detail_title_var.set(f"{payload['course']}（查询失败）")
        elif kind == "detail_idle":
            self._detail_running = False
            self.detail_refresh_btn.configure(state="normal")
        elif kind == "query_rows":
            self._ingest_rows(payload["rows"], payload.get("type", ""))
        elif kind == "query_done":
            total = payload.get("total", 0)
            if payload.get("stopped"):
                self.log(f"查询已中止，共取到 {total} 条", "warn")
            else:
                self.log(f"查询完成，共 {total} 条备选", "ok")
            self._set_query_idle()
        elif kind == "query_failed":
            self.log(f"查询失败：{payload['message']}", "err")
            self.count_var.set("查询失败")
            self._set_query_idle()
        elif kind == "query_idle":
            self._set_query_idle()
        elif kind == "batch_code":
            self._batch_code = payload["code"]
        elif kind == "poller":
            self._render_poller(payload["event"], payload["payload"])
        elif kind == "notify":
            self._notify_success(payload["course"])
        elif kind == "finished":
            stats = payload["stats"]
            self.log(f"运行结束：{stats.summary()}", "ok" if payload["success"] else "warn")
            if not payload["started"]:
                self.log("启动未完成，请检查上面的错误信息。", "err")
            self._set_busy(False)
        elif kind == "fatal":
            self.log(payload["message"], "err")
            messagebox.showerror("运行失败", payload["message"], parent=self.master)
            self._set_busy(False)
        elif kind == "idle":
            self._set_busy(False)

    # ---------------------------------------------------------- 轮询事件渲染

    def _render_poller(self, event: str, payload: dict) -> None:
        if event == "student":
            self.login_var.set(f"登录态：{payload.get('name') or ''}（{payload.get('code')}）")
        elif event == "batch":
            self.log(f"当前批次：{payload.get('batch')}", "ok")
            code = str(payload.get("batch", ""))
            if code.startswith("[") and "]" in code:
                self._batch_code = code[1 : code.index("]")]
        elif event == "start":
            self.log(f"开始轮询 {payload.get('courses')} 门课程", "muted")
        elif event == "status":
            self._update_capacity_rows(payload)
            # 如果轮询的正是中间栏显示的那门课，同步刷新中间栏
            if payload.get("course") == self._detail_course:
                self._render_detail(
                    {
                        "course": payload["course"],
                        "rows": [
                            {
                                "class": c.get("id", ""),
                                "teacher": c.get("teacher"),
                                "capacity": c.get("capacity_total"),
                                "selected": c.get("selected"),
                                "remaining": c.get("remaining"),
                                "status": c.get("status"),
                                "status_label": c.get("status_label"),
                                "place": c.get("place", ""),
                            }
                            for c in (payload.get("classes") or [])
                        ],
                    }
                )
        elif event == "attempt":
            self.log(f"→ 提交选课：{payload.get('course')} / {payload.get('class_id')}", "info")
        elif event == "dry_run_skip":
            self.log(
                f"→ [试跑] 发现余量：{payload.get('course')} / "
                f"{payload.get('class_id')} {payload.get('capacity')}（未提交）",
                "warn",
            )
        elif event == "success":
            self.log(
                f"🎉 选课成功：{payload.get('course')}（教学班 {payload.get('class_id')}）",
                "ok",
            )
        elif event == "already":
            self.log(f"已经选过：{payload.get('course')}", "ok")
        elif event == "pending":
            self.log(f"已受理待确认：{payload.get('course')} — {payload.get('message')}", "warn")
        elif event == "conflict":
            self.log(f"时间冲突，跳过：{payload.get('course')} / {payload.get('class_id')}", "warn")
        elif event == "miss":
            if payload.get("outcome") == SelectionOutcome.FULL.value:
                self.log(f"· {payload.get('course')} 已满，继续等待", "muted")
            else:
                self.log(f"· {payload.get('course')} {payload.get('message')}", "muted")
        elif event == "relogin":
            self.log("登录态失效，正在重新登录…", "warn")
        elif event == "rate_limited":
            self.log(
                f"被限流，冷却 {payload.get('cooldown')} 秒；间隔调整为 "
                f"{payload.get('interval')} 秒",
                "warn",
            )
        elif event == "server_busy":
            self.log(f"选课系统在线人数已满，冷却 {payload.get('cooldown')} 秒后重试", "warn")
        elif event == "warn":
            self.log(str(payload.get("message")), "warn")
        elif event == "error":
            self.log(str(payload.get("message")), "err")
        elif event == "timeout":
            self.log("已达到最长运行时间，自动停止。", "warn")
        elif event == "all_done":
            self.log("所有目标课程都已处理完成。", "ok")
        elif event == "interrupted":
            self.log("已手动停止。", "warn")
        elif event == "fatal":
            self.log(str(payload.get("message")), "err")

    # ================================================================ 中间栏：任务详情

    def _on_watch_selected(self, _event=None) -> None:
        """左侧选中一门课 → 拉取它的教学班列表填到中间栏。"""
        index = self._selected_course_index()
        if index is None or not self.cfg:
            return
        target = self.cfg.courses[index]
        if target.name == self._detail_course and self.detail_tree.get_children():
            return  # 已经是这门课且已有数据，不重复请求
        self._detail_course = target.name
        self.detail_title_var.set(target.name)
        self._refresh_detail(target)

    def _on_refresh_detail(self) -> None:
        index = self._selected_course_index()
        if index is None or not self.cfg:
            messagebox.showinfo("未选中课程", "请先在左侧选一门课。", parent=self.master)
            return
        target = self.cfg.courses[index]
        self._detail_course = target.name
        self.detail_title_var.set(target.name)
        self._refresh_detail(target)

    def _refresh_detail(self, target) -> None:
        if self.session is None:
            self.detail_title_var.set(f"{target.name}（未登录）")
            return
        if self._detail_running:
            return
        self._detail_running = True
        self.detail_refresh_btn.configure(state="disabled")
        cfg = self._snapshot()  # 必须在主线程取（tkinter 变量不能跨线程读）
        threading.Thread(target=self._detail_worker, args=(target, cfg), daemon=True).start()

    def _detail_worker(self, target, cfg: Config) -> None:
        from .auth import normalize_base_url
        from .client import XkClient
        from .http import HttpClient

        http = None
        try:
            http = HttpClient(
                min_interval=max(cfg.poll.min_request_interval, 0.5),
                timeout=cfg.http.timeout,
                max_retries=1,
                verify=cfg.http.verify_ssl,
                proxy=cfg.http.proxy or None,
            )
            http.cookies = dict(self.session.cookies)
            http.set_token(self.session.token)
            http.student_code = self.session.student_code
            client = XkClient(http, api_base=normalize_base_url(cfg.api_base))

            batch_code = self._batch_code
            if not batch_code:
                batch = client.current_batch(self.session.student_code)
                batch_code = batch.code
                self.events.put(_GuiEvent("batch_code", {"code": batch_code}))

            classes = client.find_teaching_classes(
                target.name,
                teaching_class_type=target.type,
                batch_code=batch_code,
                student_code=self.session.student_code,
            )
            rows = [
                {
                    "course": target.name,
                    "class": tc.teaching_class_id,
                    "teacher": tc.teacher,
                    "capacity": tc.capacity,
                    "selected": tc.selected_count,
                    "remaining": tc.remaining,
                    "status": tc.status.value,
                    "status_label": tc.status.label,
                    "place": tc.time_place,
                    "matched": target.matches(tc),
                }
                for tc in classes
            ]
            self.events.put(_GuiEvent("detail_rows", {"course": target.name, "rows": rows}))
        except BitxkError as exc:
            logger.warning("查询「%s」的教学班失败：%s", target.name, exc)
            self.events.put(
                _GuiEvent("detail_failed", {"course": target.name, "message": str(exc)})
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("查询任务详情异常")
            self.events.put(
                _GuiEvent("detail_failed", {"course": target.name, "message": f"意外错误：{exc}"})
            )
        finally:
            if http is not None:
                http.close()
            self._detail_running = False
            self.events.put(_GuiEvent("detail_idle", {}))

    def _render_detail(self, payload: dict) -> None:
        """把中间栏的查询结果画出来。"""
        course = payload.get("course", "")
        rows = payload.get("rows") or []
        # 用户可能已经切换到别的课，丢弃过期结果
        if course and course != self._detail_course:
            return

        self.detail_tree.delete(*self.detail_tree.get_children())
        for row in rows:
            tag = {
                CourseStatus.AVAILABLE.value: "available",
                CourseStatus.FULL.value: "full",
                CourseStatus.SELECTED.value: "selected",
                CourseStatus.CONFLICT.value: "conflict",
            }.get(row.get("status"), "")
            self.detail_tree.insert(
                "",
                "end",
                values=(
                    row.get("class", ""),
                    row.get("teacher") or "-",
                    "-" if row.get("capacity") is None else row["capacity"],
                    "-" if row.get("selected") is None else row["selected"],
                    "-" if row.get("remaining") is None else row["remaining"],
                    row.get("status_label", ""),
                    (row.get("place") or "")[:28],
                ),
                tags=(tag,) if tag else (),
            )

        if not rows:
            self.detail_title_var.set(f"{course}（未找到匹配的教学班）")
        else:
            avail = sum(1 for r in rows if r.get("status") == CourseStatus.AVAILABLE.value)
            tail = (
                f"  共 {len(rows)} 个班，{avail} 个有余量"
                if avail
                else f"  共 {len(rows)} 个班，均已满"
            )
            self.detail_title_var.set(f"{course}{tail}")

    # ================================================================ 课程查询

    def _on_query_courses(self) -> None:
        """查询全部备选课程（后台线程，不阻塞界面）。"""
        if self.session is None:
            messagebox.showinfo("还没有登录态", "请先点「用浏览器登录」。", parent=self.master)
            return
        if self._query_running:
            return

        keyword = self.search_var.get().strip()
        if self.query_all_var.get():
            types = list(CourseType.ALL)
        else:
            types = [self._selected_type() or CourseType.XGXK]

        self._query_running = True
        self._query_stop.clear()
        self.query_btn.configure(state="disabled")
        self.stop_query_btn.configure(state="normal")
        self.count_var.set("查询中…")
        self.log(f"开始查询课程：{keyword or '（全部）'} / {len(types)} 种类型", "info")
        cfg = self._snapshot()  # 同上，主线程取快照
        threading.Thread(target=self._query_worker, args=(keyword, types, cfg), daemon=True).start()

    def _on_stop_query(self) -> None:
        self._query_stop.set()
        self.log("已请求停止查询…", "warn")

    def _selected_type(self) -> str:
        """把下拉框的显示文本还原成 teachingClassType。"""
        text = self.type_var.get().strip()
        if not text or text == "全部":
            return ""
        return text.split()[0]

    def _query_worker(self, keyword: str, types: list[str], cfg: Config) -> None:
        """后台：逐类型查询，把结果推回主线程。"""
        from .auth import normalize_base_url
        from .client import XkClient
        from .http import HttpClient

        http = None
        try:
            http = HttpClient(
                min_interval=max(cfg.poll.min_request_interval, 0.5),
                timeout=cfg.http.timeout,
                max_retries=1,
                verify=cfg.http.verify_ssl,
                proxy=cfg.http.proxy or None,
            )
            http.cookies = dict(self.session.cookies)
            http.set_token(self.session.token)
            http.student_code = self.session.student_code
            client = XkClient(http, api_base=normalize_base_url(cfg.api_base))

            # 批次 / 校区从会话里取，取不到再问接口
            batch_code = self._batch_code
            if not batch_code:
                batch = client.current_batch(self.session.student_code)
                batch_code = batch.code
                self.events.put(_GuiEvent("batch_code", {"code": batch_code}))

            total = 0
            for tc_type in types:
                if self._query_stop.is_set():
                    break
                try:
                    courses = client.query_courses(
                        keyword,
                        teaching_class_type=tc_type,
                        batch_code=batch_code,
                        student_code=self.session.student_code,
                        page_size=100,
                        check_capacity="2",
                        check_conflict="0",
                    )
                except BitxkError as exc:
                    self._log_threadsafe(f"  {CourseType.label(tc_type)} 查询失败：{exc}", "warn")
                    continue

                rows = []
                for course in courses:
                    for tc in course.teaching_classes:
                        rows.append(
                            {
                                "course": course.name or tc.course_name,
                                "type": tc_type,
                                "class": tc.teaching_class_id,
                                "teacher": tc.teacher,
                                "place": tc.time_place,
                                "capacity": tc.capacity,
                                "remaining": tc.remaining,
                                "selected": tc.selected_count,
                                "status": tc.status.value,
                                "status_label": tc.status.label,
                                "capacity_text": tc.capacity_text,
                                "key": f"{tc_type}:{tc.teaching_class_id}",
                            }
                        )
                total += len(rows)
                self.events.put(_GuiEvent("query_rows", {"rows": rows, "type": tc_type}))

            self.events.put(
                _GuiEvent("query_done", {"total": total, "stopped": self._query_stop.is_set()})
            )
        except BitxkError as exc:
            self.events.put(_GuiEvent("query_failed", {"message": str(exc)}))
        except Exception as exc:  # pragma: no cover
            logger.exception("课程查询异常")
            self.events.put(_GuiEvent("query_failed", {"message": f"意外错误：{exc}"}))
        finally:
            if http is not None:
                http.close()
            self._query_running = False
            self.events.put(_GuiEvent("query_idle", {}))

    def _ingest_rows(self, rows: list[dict], tc_type: str = "") -> None:
        """把一批查询结果并入表格模型（同 key 覆盖，保留其它类型的结果）。"""
        index = {r["key"]: r for r in self._rows}
        for row in rows:
            index[row["key"]] = row
        self._rows = list(index.values())
        self._apply_filters()

    # ================================================================ 筛选与排序

    def _on_clear_filters(self) -> None:
        self.search_var.set("")
        self.type_var.set("全部")
        self.filter_available_var.set(False)
        self.filter_full_var.set(False)
        self.filter_conflict_var.set(False)
        self.filter_selected_var.set(False)
        self._sort_key = None
        self._sort_desc = False
        self._apply_filters()

    def _on_sort_column(self, key: str) -> None:
        """点列标题：同列再点一次切换升降序。"""
        if self._sort_key == key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key, self._sort_desc = key, False
        self._apply_filters()

    def _apply_filters(self) -> None:
        """按关键词 + 类型 + 勾选项过滤，然后重画表格（纯本地，无请求）。"""
        if not hasattr(self, "_rows"):
            return

        keyword = self.search_var.get().strip().lower()
        type_filter = self._selected_type()
        want_available = self.filter_available_var.get()
        want_full = self.filter_full_var.get()
        want_conflict = self.filter_conflict_var.get()
        want_selected = self.filter_selected_var.get()
        any_status_filter = want_available or want_full or want_conflict or want_selected

        rows = []
        for row in self._rows:
            if (
                keyword
                and keyword not in row["course"].lower()
                and keyword not in (row.get("teacher") or "").lower()
                and keyword not in row["class"].lower()
            ):
                continue
            if type_filter and row["type"] != type_filter:
                continue
            if any_status_filter:
                ok = (
                    (want_available and row["status"] == CourseStatus.AVAILABLE.value)
                    or (want_full and row["status"] == CourseStatus.FULL.value)
                    or (want_conflict and row["status"] == CourseStatus.CONFLICT.value)
                    or (want_selected and row["status"] == CourseStatus.SELECTED.value)
                )
                if not ok:
                    continue
            rows.append(row)

        # 排序
        if self._sort_key:
            key = self._sort_key
            if key == "remaining":
                rows.sort(
                    key=lambda r: (r["remaining"] is None, r["remaining"]), reverse=self._sort_desc
                )
            elif key == "capacity":
                rows.sort(
                    key=lambda r: (r["capacity"] is None, r["capacity"]), reverse=self._sort_desc
                )
            else:
                rows.sort(key=lambda r: str(r.get(key) or ""), reverse=self._sort_desc)
        else:
            # 默认：有余量的排前面，再按课程名
            rows.sort(key=lambda r: (r["status"] != CourseStatus.AVAILABLE.value, r["course"]))

        self._render_rows(rows)

    def _render_rows(self, rows: list[dict]) -> None:
        self.cap_tree.delete(*self.cap_tree.get_children())
        stamp = time.strftime("%H:%M:%S")
        for row in rows:
            tag = {
                CourseStatus.AVAILABLE.value: "available",
                CourseStatus.FULL.value: "full",
                CourseStatus.SELECTED.value: "selected",
                CourseStatus.CONFLICT.value: "conflict",
            }.get(row["status"], "")
            self.cap_tree.insert(
                "",
                "end",
                iid=row["key"],
                values=(
                    row["course"],
                    CourseType.label(row["type"]),
                    row["class"],
                    row.get("teacher") or "-",
                    (row.get("place") or "")[:30],
                    row.get("capacity_text") or "-",
                    "-" if row.get("remaining") is None else row["remaining"],
                    row["status_label"],
                    stamp,
                ),
                tags=(tag,) if tag else (),
            )
        total, shown = len(self._rows), len(rows)
        if total == 0:
            self.count_var.set("尚未查询")
        else:
            self.count_var.set(f"显示 {shown} / 共 {total} 条")

    # ================================================================ 轮询结果并入

    def _on_watch_selected_row(self, _event=None) -> None:
        """双击表格里的一行，把该课程加进「要盯的课程」。"""
        selection = self.cap_tree.selection()
        if not selection or not self.cfg:
            return
        row = next((r for r in self._rows if r["key"] == selection[0]), None)
        if not row:
            return
        name = row["course"]
        if any(t.name == name for t in self.cfg.courses):
            self.log(f"「{name}」已经在要盯的列表里了", "muted")
            return
        try:
            target = WatchTarget(name=name, type=row["type"], priority=100)
        except ConfigError as exc:
            self.log(f"无法添加：{exc}", "err")
            return
        self.cfg.courses.append(target)
        self._refresh_course_tree()
        self.log(f"已加入要盯的课程：{name}（{CourseType.label(row['type'])}）", "ok")

    def _update_capacity_rows(self, payload: dict) -> None:
        """轮询时把最新状态并入同一张表（按教学班 ID 覆盖）。"""
        course = payload.get("course", "")
        classes = payload.get("classes") or []
        # 注意：轮询事件里没有课程类型，按教学班 ID 匹配已有行；
        # 匹配不到就当作新行加入（类型留空）。
        by_class = {r["class"]: r for r in self._rows}
        for item in classes:
            tc_id = str(item.get("id", ""))
            existing = by_class.get(tc_id)
            key = existing["key"] if existing else f"poll:{tc_id}"
            row = existing or {
                "course": course,
                "type": "",
                "class": tc_id,
                "key": key,
            }
            row.update(
                {
                    "teacher": item.get("teacher") or row.get("teacher", ""),
                    "status": item.get("status", CourseStatus.UNKNOWN.value),
                    "status_label": item.get("status_label", ""),
                    "capacity_text": item.get("capacity", ""),
                    "remaining": item.get("remaining"),
                }
            )
            by_class[tc_id] = row

        if classes:
            # 用 by_class 的值重建（已在表里的其它课程保留）
            merged = {r["key"]: r for r in self._rows}
            for row in by_class.values():
                merged[row["key"]] = row
            self._rows = list(merged.values())
        self._apply_filters()

    def _notify_success(self, course: str) -> None:
        from .notify import Notify

        notifier = Notify(sound=True)
        notifier.success(course)
        messagebox.showinfo(
            "抢到课了",
            f"🎉 {course} 选课成功！\n\n请到选课系统确认一下。",
            parent=self.master,
        )

    # ================================================================ 状态

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.start_btn.configure(state=state)
        self.browser_btn.configure(state=state)
        if busy:
            self.stop_btn.configure(state="normal")
        else:
            self.stop_btn.configure(state="disabled")
            self.poller = None

    def _set_query_idle(self) -> None:
        self._query_running = False
        self.query_btn.configure(state="normal")
        self.stop_query_btn.configure(state="disabled")

    def _on_close(self) -> None:
        if self._busy and not messagebox.askyesno(
            "正在运行", "抢课还在进行中，确定要退出吗？", parent=self.master
        ):
            return
        self._stop_flag.set()
        if self.poller is not None:
            self.poller.stop()
        self.master.destroy()


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


def _wrap_poller_events(base_handler, on_success):
    """在轮询事件流里额外捕获 success，用于弹窗与提示音。"""

    def handler(event: str, payload: dict) -> None:
        base_handler(event, payload)
        if event == "success":
            on_success(str(payload.get("course", "")), str(payload.get("message", "")))

    return handler


def _has_font(name: str) -> bool:
    import tkinter.font as tkfont

    try:
        return name in tkfont.families()
    except tk.TclError:  # pragma: no cover
        return False


def save_config(cfg: Config, path: str | Path) -> None:
    """把界面上的课程与轮询参数写回 TOML 配置文件。

    刻意只写"用户资产"（账号、参数、课程），不写运行时状态。
    密码不落盘：如果原文件里没有密码，这里也不会凭空写一个进去。
    """
    path = Path(path)
    lines: list[str] = [
        "# 由 BIT 选课助手 GUI 保存",
        "",
        f'api_base = "{_escape(cfg.api_base)}"',
        "",
        "[account]",
        f'username = "{_escape(cfg.username)}"',
        f'password = "{_escape(cfg.password)}"',
        "",
        "[poll]",
        f"interval = {cfg.poll.interval}",
        f"min_request_interval = {cfg.poll.min_request_interval}",
        f"jitter = {cfg.poll.jitter}",
        f"max_duration = {cfg.poll.max_duration}",
        f"max_consecutive_errors = {cfg.poll.max_consecutive_errors}",
        f"rate_limit_cooldown = {cfg.poll.rate_limit_cooldown}",
        f"relogin_after = {cfg.poll.relogin_after}",
        "",
        "[http]",
        f"timeout = {cfg.http.timeout}",
        f"max_retries = {cfg.http.max_retries}",
        f'proxy = "{_escape(cfg.http.proxy)}"',
        f"verify_ssl = {str(cfg.http.verify_ssl).lower()}",
        "",
        "[notify]",
        f"sound = {str(cfg.notify.sound).lower()}",
        f"stop_on_success = {str(cfg.notify.stop_on_success).lower()}",
        "",
    ]

    for target in cfg.courses:
        lines.append("[[courses]]")
        lines.append(f'name = "{_escape(target.name)}"')
        lines.append(f'type = "{target.type}"')
        lines.append(f"priority = {target.priority}")
        if target.teachers:
            teachers = ", ".join(f'"{_escape(t)}"' for t in target.teachers)
            lines.append(f"teachers = [{teachers}]")
        if target.classes:
            classes = ", ".join(f'"{_escape(c)}"' for c in target.classes)
            lines.append(f"classes = [{classes}]")
        lines.append(f"enabled = {str(target.enabled).lower()}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    with contextlib.suppress(Exception):
        path.chmod(0o600)  # 可能含密码，收紧权限


def _escape(text: str) -> str:
    """转义成 TOML 基本字符串里的安全内容。"""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def run_gui(config_path: str | Path | None = None, *, title: str = "BIT 选课助手") -> int:
    """启动图形界面。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - 无显示环境
        raise BitxkError(
            f"无法启动图形界面（{exc}）。\n如果你在纯命令行环境（如 SSH），请改用 bitxk grab。"
        ) from exc

    root.title(title)
    root.geometry("1080x760")
    root.minsize(900, 640)
    with contextlib.suppress(Exception):
        root.tk.call("tk", "scaling", 1.25)

    BitxkApp(root, config_path=config_path)
    root.mainloop()
    return 0
