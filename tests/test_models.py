"""数据模型测试：容量推断与状态判定是轮询正确性的根基，必须重点覆盖。"""

from __future__ import annotations

import pytest

from bitxk.models import (
    Batch,
    Course,
    CourseStatus,
    SelectionOutcome,
    SelectionResult,
    TeachingClass,
    _as_bool,
    _as_int,
    _pick,
)


class TestPick:
    def test_匹配忽略大小写与下划线(self):
        data = {"teachingClassID": "abc123"}
        assert _pick(data, "teaching_class_id") == "abc123"
        assert _pick(data, "TEACHINGCLASSID") == "abc123"

    def test_按候选顺序取第一个非空值(self):
        data = {"a": None, "b": "", "c": "hit"}
        assert _pick(data, "a", "b", "c") == "hit"

    def test_全部缺失时返回默认值(self):
        assert _pick({}, "x", "y", default="fallback") == "fallback"

    def test_非字典输入不报错(self):
        assert _pick(None, "x", default=1) == 1


class TestAsInt:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (5, 5),
            (5.9, 5),
            ("12", 12),
            ("余 28", 28),
            ("-3", -3),
            ("", None),
            (None, None),
            ("没有数字", None),
            (True, None),  # 布尔不当数字，避免 True 变成 1 污染容量
        ],
    )
    def test_转换(self, value, expected):
        assert _as_int(value) == expected


class TestAsBool:
    @pytest.mark.parametrize(
        "value,expected",
        [("1", True), (1, True), ("true", True), ("Y", True), ("0", False), (None, False), ("no", False)],
    )
    def test_转换(self, value, expected):
        assert _as_bool(value) is expected


class TestCapacityInference:
    """不同接口返回的容量字段命名不一致，这里验证多路推断。"""

    def test_直接给剩余容量(self):
        tc = TeachingClass.from_api({"teachingClassID": "1", "remainCapacity": 5, "capacity": 40})
        assert tc.remaining == 5
        assert tc.capacity_source == "remaining"
        assert tc.status is CourseStatus.AVAILABLE

    def test_由容量减已选推算(self):
        tc = TeachingClass.from_api({"teachingClassID": "1", "capacity": 40, "selectedCount": 40})
        assert tc.remaining == 0
        assert tc.capacity_source == "derived"
        assert tc.status is CourseStatus.FULL

    def test_中文拼音字段名(self):
        # 部分版本用「容量 rl / 已选 yxrs」这类缩写
        tc = TeachingClass.from_api({"teachingClassID": "1", "rl": 30, "yxrs": 12})
        assert tc.remaining == 18
        assert tc.status is CourseStatus.AVAILABLE

    def test_容量字段单独存在时视为可选数(self):
        tc = TeachingClass.from_api({"teachingClassID": "1", "remainNumber": 3})
        assert tc.remaining == 3

    def test_无容量信息时状态未知(self):
        tc = TeachingClass.from_api({"teachingClassID": "1", "courseName": "某课"})
        assert tc.remaining is None
        assert tc.status is CourseStatus.UNKNOWN
        assert tc.capacity_text == "容量未知"

    def test_已选标记优先于容量(self):
        tc = TeachingClass.from_api({"teachingClassID": "1", "remainCapacity": 0, "isSelected": "1"})
        assert tc.status is CourseStatus.SELECTED

    def test_文本里出现冲突字样(self):
        tc = TeachingClass.from_api({"teachingClassID": "1", "remainCapacity": 5, "remark": "时间冲突"})
        assert tc.status is CourseStatus.CONFLICT

    def test_满员时不可选(self):
        tc = TeachingClass.from_api({"teachingClassID": "1", "remainCapacity": 0})
        assert tc.status is CourseStatus.FULL
        assert not tc.is_selectable

    def test_容量展示文本(self):
        tc = TeachingClass.from_api({"teachingClassID": "1", "capacity": 40, "selectedCount": 12})
        assert tc.capacity_text == "12/40 (余 28)"


class TestTeachingClassBasics:
    def test_缺id时构造空串而不报错(self):
        tc = TeachingClass.from_api({"courseName": "某课"})
        assert tc.teaching_class_id == ""

    def test_非字典输入报错(self):
        with pytest.raises(TypeError):
            TeachingClass.from_api(["not", "a", "dict"])  # type: ignore[arg-type]

    def test_保留原始数据便于排错(self):
        raw = {"teachingClassID": "1", "weirdField": "神秘值"}
        tc = TeachingClass.from_api(raw)
        assert tc.raw["weirdField"] == "神秘值"


class TestCourse:
    def test_展开教学班列表(self):
        data = {
            "courseName": "科幻文学",
            "tcList": [
                {"teachingClassID": "100", "teacherName": "张三", "remainCapacity": 2},
                {"teachingClassID": "101", "teacherName": "李四", "remainCapacity": 0},
            ],
        }
        course = Course.from_api(data)
        assert course.name == "科幻文学"
        assert len(course.teaching_classes) == 2
        assert course.teaching_classes[0].course_name == "科幻文学"
        assert course.teaching_classes[0].teacher == "张三"

    def test_教学班字段平铺在课程对象上(self):
        course = Course.from_api({"courseName": "体育", "teachingClassID": "200", "remainCapacity": 1})
        assert len(course.teaching_classes) == 1
        assert course.teaching_classes[0].teaching_class_id == "200"

    def test_可迭代(self):
        course = Course.from_api({"courseName": "x", "tcList": [{"teachingClassID": "1"}]})
        assert [tc.teaching_class_id for tc in course] == ["1"]

    def test_无教学班时为空列表(self):
        course = Course.from_api({"courseName": "查无此课"})
        assert course.teaching_classes == []


class TestBatch:
    def test_解析可选批次(self):
        batch = Batch.from_api({
            "code": "B2024",
            "name": "第一轮",
            "canSelect": "1",
            "schoolTermName": "2024-2025-1",
            "beginTime": "2024-09-01 08:00",
            "endTime": "2024-09-05 18:00",
        })
        assert batch.code == "B2024"
        assert batch.can_select is True
        assert "可选" in str(batch)

    def test_不可选批次(self):
        batch = Batch.from_api({"code": "B", "canSelect": "0"})
        assert batch.can_select is False


class TestSelectionResult:
    def test_成功判断(self):
        result = SelectionResult(outcome=SelectionOutcome.SUCCESS, message="选课成功")
        assert result.ok
        assert "选课成功" in str(result)

    @pytest.mark.parametrize(
        "outcome,retryable",
        [
            (SelectionOutcome.FULL, True),
            (SelectionOutcome.RATE_LIMITED, True),
            (SelectionOutcome.ERROR, True),
            (SelectionOutcome.NOT_IN_BATCH, True),
            (SelectionOutcome.SUCCESS, False),
            (SelectionOutcome.CONFLICT, False),
            (SelectionOutcome.ALREADY, False),
            (SelectionOutcome.AUTH_ERROR, False),
        ],
    )
    def test_可重试语义(self, outcome, retryable):
        """满员要继续轮询，冲突和已选则不该再试。"""
        assert SelectionResult(outcome=outcome).outcome.should_retry is retryable
