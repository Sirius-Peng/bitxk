"""数据模型：课程、教学班、批次、选课结果。

设计原则
--------
选课系统的响应字段命名不稳定（同一语义在不同批次/不同课程类型下
字段名可能不同），因此这里不把任何单一字段名当作唯一真相，而是：

1. 用 ``_pick()`` 在一组候选键名中做大小写不敏感、忽略下划线的匹配；
2. 保留一份 ``raw`` 原始字典，便于现场排错与后续适配；
3. 对「剩余容量」这种关键值，在 :class:`TeachingClass` 里做多路推断，
   并显式暴露 ``capacity_source`` 说明这个数是从哪个字段推出来的。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "CourseStatus",
    "TeachingClass",
    "Course",
    "Batch",
    "SelectionResult",
    "SelectionOutcome",
]


def _norm(key: str) -> str:
    """把字段名归一化：转小写并去掉下划线与连字符。"""
    return re.sub(r"[_\-\s]", "", str(key)).lower()


def _pick(data: dict, *candidates: str, default: Any = None) -> Any:
    """在一组候选键名中取值（大小写/下划线不敏感），取不到返回 default。"""
    if not isinstance(data, dict):
        return default
    table = {_norm(k): v for k, v in data.items()}
    for cand in candidates:
        if _norm(cand) in table:
            value = table[_norm(cand)]
            if value not in (None, ""):
                return value
    return default


def _as_int(value: Any) -> int | None:
    """尽力把任意值转成整数；失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"-?\d+", text)
    return int(match.group()) if match else None


def _as_bool(value: Any) -> bool:
    """尽力把 ``'1'`` / ``'true'`` / ``1`` 这类值转成布尔。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "y", "yes", "是"}


class CourseStatus(str, Enum):
    """教学班对当前用户的可选状态。"""

    AVAILABLE = "available"  # 有余量，可以选
    FULL = "full"  # 已满（需要轮询等待）
    SELECTED = "selected"  # 已经选过
    CONFLICT = "conflict"  # 与已选课程时间冲突
    QUEUED = "queued"  # 队列处理中，暂不可提交
    UNKNOWN = "unknown"  # 信息不足，无法判断

    @property
    def label(self) -> str:
        return {
            CourseStatus.AVAILABLE: "有余量",
            CourseStatus.FULL: "已满",
            CourseStatus.SELECTED: "已选",
            CourseStatus.CONFLICT: "冲突",
            CourseStatus.QUEUED: "队列中",
            CourseStatus.UNKNOWN: "未知",
        }[self]


@dataclass
class TeachingClass:
    """一个教学班（选课提交的最小单位）。"""

    teaching_class_id: str
    """教学班 ID —— 提交选课时 ``teachingClassId`` 用的就是它。"""

    course_name: str = ""
    teacher: str = ""
    campus: str = ""
    time_place: str = ""
    credits: str = ""

    capacity: int | None = None
    """容量上限。"""

    selected_count: int | None = None
    """已选人数。"""

    remaining: int | None = None
    """剩余容量（核心指标，轮询看的就是它）。"""

    capacity_source: str = "unknown"
    """``remaining`` 的来源，取值：``remaining`` / ``derived`` / ``unknown``。

    - ``remaining``：响应里直接给了剩余数（少见）；
    - ``derived``：由 ``classCapacity - 已选人数`` 推算（**本系统的常态**）；
    - ``unknown``：没拿到容量信息。
    """

    status: CourseStatus = CourseStatus.UNKNOWN
    raw: dict = field(default_factory=dict, repr=False)

    # ---------------------------------------------------------------- 构造

    @classmethod
    def from_api(cls, data: dict, *, course_name: str = "") -> TeachingClass:
        """从查询接口返回的一个教学班字典构造对象。"""
        if not isinstance(data, dict):
            raise TypeError("教学班数据必须是 dict")

        tc_id = _pick(
            data,
            "teachingClassID",
            "teachingClassId",
            "teachingclassid",
            "jxbid",
            "classId",
            "id",
        )
        cls_name = _pick(data, "courseName", "kcmc", "course_name", default=course_name)

        obj = cls(
            teaching_class_id=str(tc_id) if tc_id is not None else "",
            course_name=str(cls_name or course_name or ""),
            teacher=str(
                _pick(data, "teacherName", "teachers", "skjsxm", "teacher", default="") or ""
            ),
            campus=str(_pick(data, "campusName", "campus", "xqmc", default="") or ""),
            time_place=str(
                _pick(data, "timePlace", "sksjdd", "classTimePlace", "arrangeInfo", default="")
                or ""
            ),
            credits=str(_pick(data, "credits", "xf", default="") or ""),
            raw=dict(data),
        )
        obj._infer_capacity()
        obj._infer_status()
        return obj

    def _infer_capacity(self) -> None:
        """推断容量信息。

        重要：这套接口**没有任何 ``remaining`` 字段**，剩余量是前端算出来的::

            剩余 = classCapacity - numberOfFirstVolunteer

        ``dataList[]`` 顶层项用 ``numberOfFirstVolunteer``（只统计第一志愿），
        ``tcList[]`` 子项用 ``numberOfSelected``（已选总数，所有志愿合计）。
        两者语义不同不能混用，所以这里按「当前字典里实际存在哪个字段」来选。

        结论来自对生产前端 ``grablessons.js`` 的源码分析，不是猜测。
        """
        raw = self.raw

        capacity = _as_int(
            _pick(
                raw,
                "classCapacity",
                "capacity",
                "totalCapacity",
                "limitCount",
                "maxCount",
                "number",
                "rl",
                "zrs",
                "total",
                "limit",
            )
        )

        # 已选人数：优先「已选总数」，退化到「第一志愿人数」
        selected = _as_int(
            _pick(
                raw,
                "numberOfSelected",
                "numberOfFirstVolunteer",
                "selectedCount",
                "selectedNumber",
                "selectedNum",
                "chosenCount",
                "yxrs",
                "selected",
            )
        )

        # 极少数接口版本直接给剩余量；真给到就优先信任
        direct_remaining = _as_int(
            _pick(
                raw,
                "remainCapacity",
                "remainingCapacity",
                "remainNumber",
                "remainNum",
                "surplusCapacity",
                "leftCapacity",
                "remaining",
                "remain",
                "surplus",
                "left",
                "kyrs",
                "kyl",
            )
        )

        self.capacity = capacity
        self.selected_count = selected

        if direct_remaining is not None:
            self.remaining, self.capacity_source = direct_remaining, "remaining"
        elif capacity is not None and selected is not None:
            self.remaining, self.capacity_source = capacity - selected, "derived"
        else:
            self.remaining, self.capacity_source = None, "unknown"

    def _infer_status(self) -> None:
        """推断可选状态。

        判定优先级完全对齐生产前端：服务端给出的布尔标志
        （``isFull`` / ``isChoose`` / ``isConflict``）**比我们自己算的数字更权威** ——
        志愿制轮次下人数统计口径可能与最终录取口径不同。
        """
        raw = self.raw

        # 1) 已选 / 冲突 —— 服务端标志优先
        if _as_bool(_pick(raw, "isChoose", "isSelected", "selectedFlag", "hasSelected")):
            self.status = CourseStatus.SELECTED
            return
        if _as_bool(_pick(raw, "isConflict", "conflictFlag", "ctFlag")):
            self.status = CourseStatus.CONFLICT
            return

        # 2) 队列处理中：此时提交会被拒绝，视作不可选
        if _as_bool(_pick(raw, "inQuene", "inQueue")):
            self.status = CourseStatus.QUEUED
            return

        # 3) 已满 —— isFull 是服务端算好的，最可靠
        if _as_bool(_pick(raw, "isFull")):
            self.status = CourseStatus.FULL
            return

        # 4) 退化到文本与数值判断
        text = " ".join(str(v) for v in raw.values() if isinstance(v, (str, int, float)))
        if any(word in text for word in ("已选", "已选中", "已经选")):
            self.status = CourseStatus.SELECTED
            return
        if "冲突" in text:
            self.status = CourseStatus.CONFLICT
            return

        if self.remaining is None:
            self.status = CourseStatus.UNKNOWN
        elif self.remaining > 0:
            self.status = CourseStatus.AVAILABLE
        else:
            self.status = CourseStatus.FULL

    # ---------------------------------------------------------------- 展示

    @property
    def is_selectable(self) -> bool:
        """是否处于「可以尝试提交选课」的状态。"""
        return self.status is CourseStatus.AVAILABLE

    @property
    def capacity_text(self) -> str:
        """人类可读的容量描述，例如 ``12/40 (余 28)``。"""
        if self.remaining is None:
            return "容量未知"
        if self.capacity is None:
            return f"余 {self.remaining}"
        used = (
            self.selected_count
            if self.selected_count is not None
            else self.capacity - self.remaining
        )
        return f"{used}/{self.capacity} (余 {self.remaining})"

    def __str__(self) -> str:
        who = f" {self.teacher}" if self.teacher else ""
        return f"[{self.teaching_class_id}]{who} {self.capacity_text} {self.status.label}"


@dataclass
class Course:
    """一门课程，可能包含多个教学班。"""

    name: str
    teaching_classes: list[TeachingClass] = field(default_factory=list)
    teaching_class_type: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict, *, teaching_class_type: str = "") -> Course:
        """从查询接口返回的一门课构造对象（含 ``tcList`` 展开）。"""
        if not isinstance(data, dict):
            raise TypeError("课程数据必须是 dict")

        name = str(_pick(data, "courseName", "kcmc", "course_name", default="") or "")

        classes: list[TeachingClass] = []
        # 有的接口直接把教学班信息平铺在课程对象上，有的放在 tcList 里。
        nested = _pick(data, "tcList", "teachingClassList", "classList", "jxbList")
        if isinstance(nested, list) and nested:
            for item in nested:
                if isinstance(item, dict):
                    # 课程级字段补进教学班，避免教学班缺课程名/学分。
                    merged = {
                        k: v for k, v in data.items() if k not in ("tcList", "teachingClassList")
                    }
                    merged.update(item)
                    classes.append(TeachingClass.from_api(merged, course_name=name))
        elif _pick(data, "teachingClassID", "teachingClassId", "jxbid") is not None:
            classes.append(TeachingClass.from_api(data, course_name=name))

        return cls(
            name=name,
            teaching_classes=classes,
            teaching_class_type=teaching_class_type,
            raw=dict(data),
        )

    def __iter__(self) -> Iterator[TeachingClass]:
        return iter(self.teaching_classes)


@dataclass
class Batch:
    """选课批次（轮次）。只有在可选批次内才能提交选课。"""

    code: str
    name: str = ""
    can_select: bool = False
    school_term: str = ""
    begin_time: str = ""
    end_time: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict) -> Batch:
        return cls(
            code=str(_pick(data, "code", "batchCode", "electiveBatchCode", default="") or ""),
            name=str(_pick(data, "name", "batchName", default="") or ""),
            can_select=_as_bool(_pick(data, "canSelect", "canChoose", "selectFlag")),
            school_term=str(_pick(data, "schoolTermName", "term", "xqmc", default="") or ""),
            begin_time=str(_pick(data, "beginTime", "startTime", default="") or ""),
            end_time=str(_pick(data, "endTime", "stopTime", default="") or ""),
            raw=dict(data),
        )

    def __str__(self) -> str:
        flag = "可选" if self.can_select else "不可选"
        window = f" {self.begin_time}~{self.end_time}" if self.begin_time else ""
        return f"[{self.code}] {self.school_term} {self.name} ({flag}){window}"


class SelectionOutcome(str, Enum):
    """一次选课提交的结果分类。

    必须区分三个"还没定论"的状态：

    * ``ACCEPTED`` —— 接口已受理（``volunteer.do`` 返回 ``code=='1'``），
      后台仍在处理；
    * ``PENDING``  —— 已受理但轮询超时，结果未知，**不等于失败**
      （后台可能仍在处理，下次轮询课程列表即可确认）；
    * ``SUCCESS``  —— 已轮询 ``studentstatus.do`` 并确认成功。

    这套系统的选课是**异步**的，所以三者必须分开表达，
    否则会把"还在处理"误报成"抢到了"或"失败了"。
    """

    SUCCESS = "success"  # 已确认选上
    ACCEPTED = "accepted"  # 请求已受理，结果未定
    PENDING = "pending"  # 受理后轮询超时，结果未知
    FULL = "full"  # 容量已满（继续轮询）
    CONFLICT = "conflict"  # 时间冲突（轮询无意义）
    ALREADY = "already"  # 已经选过
    NOT_IN_BATCH = "not_batch"  # 不在可选批次 / 批次未开放
    AUTH_ERROR = "auth_error"  # 登录态失效，需要重新登录
    RATE_LIMITED = "rate_limited"  # 被限流
    ERROR = "error"  # 其它错误

    @property
    def label(self) -> str:
        return {
            SelectionOutcome.SUCCESS: "选课成功",
            SelectionOutcome.ACCEPTED: "已受理待确认",
            SelectionOutcome.PENDING: "结果未知",
            SelectionOutcome.FULL: "容量已满",
            SelectionOutcome.CONFLICT: "时间冲突",
            SelectionOutcome.ALREADY: "已经选过",
            SelectionOutcome.NOT_IN_BATCH: "批次不可用",
            SelectionOutcome.AUTH_ERROR: "登录失效",
            SelectionOutcome.RATE_LIMITED: "被限流",
            SelectionOutcome.ERROR: "未知错误",
        }[self]

    @property
    def should_retry(self) -> bool:
        """是否值得继续轮询重试。"""
        return self in {
            SelectionOutcome.FULL,
            SelectionOutcome.RATE_LIMITED,
            SelectionOutcome.ERROR,
            SelectionOutcome.NOT_IN_BATCH,
        }

    @property
    def is_settled(self) -> bool:
        """是否已有明确结论（不需要再等）。"""
        return self in {SelectionOutcome.SUCCESS, SelectionOutcome.ALREADY}


@dataclass
class SelectionResult:
    """选课提交的解析结果。"""

    outcome: SelectionOutcome
    message: str = ""
    code: Any = None
    teaching_class_id: str = ""
    course_name: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def ok(self) -> bool:
        return self.outcome is SelectionOutcome.SUCCESS

    def __str__(self) -> str:
        who = f"[{self.teaching_class_id}] {self.course_name}".strip()
        return f"{self.outcome.label}: {who} {self.message}".strip()
