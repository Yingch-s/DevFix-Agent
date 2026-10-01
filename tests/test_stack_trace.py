"""stack_trace 解析器测试。"""

from __future__ import annotations

from devfix.parsing.stack_trace import parse_stack_traces
from tests.sample_logs import JUNIT4_NPE_ERROR, JUNIT5_ASSERTION_FAILURE


class TestBasicParsing:
    def test_single_trace_frames(self) -> None:
        text = (
            "java.lang.IllegalArgumentException: bad id\n"
            "\tat com.example.Foo.bar(Foo.java:10)\n"
            "\tat com.example.Baz.qux(Baz.java:20)\n"
        )
        traces = parse_stack_traces(text)
        assert len(traces) == 1
        t = traces[0]
        assert t.exception_type == "java.lang.IllegalArgumentException"
        assert t.message == "bad id"
        assert len(t.frames) == 2
        assert t.frames[0].class_name == "com.example.Foo"
        assert t.frames[0].method_name == "bar"
        assert t.frames[0].file_name == "Foo.java"
        assert t.frames[0].line_number == 10
        assert t.frames[0].location == "Foo.java:10"

    def test_native_method_and_unknown_source(self) -> None:
        text = (
            "java.lang.RuntimeException: boom\n"
            "\tat com.example.A.m(A.java:1)\n"
            "\tat com.example.B.n(Native Method)\n"
            "\tat com.example.C.p(Unknown Source)\n"
        )
        frames = parse_stack_traces(text)[0].frames
        assert frames[1].file_name is None and frames[1].line_number is None
        assert frames[2].file_name is None and frames[2].line_number is None

    def test_module_prefix(self) -> None:
        text = (
            "java.lang.Exception: x\n"
            "\tat java.base/java.util.ArrayList.forEach(ArrayList.java:1511)\n"
        )
        frame = parse_stack_traces(text)[0].frames[0]
        assert frame.module_name == "java.base"
        assert frame.class_name == "java.util.ArrayList"
        assert frame.method_name == "forEach"


class TestCausedByChain:
    def test_caused_by_attached_as_cause(self) -> None:
        text = (
            "java.lang.NullPointerException: outer\n"
            "\tat com.example.Outer.call(Outer.java:10)\n"
            "Caused by: java.lang.IllegalStateException: inner\n"
            "\tat com.example.Inner.run(Inner.java:20)\n"
        )
        traces = parse_stack_traces(text)
        assert len(traces) == 1
        t = traces[0]
        assert t.exception_type == "java.lang.NullPointerException"
        assert t.cause is not None
        assert t.cause.exception_type == "java.lang.IllegalStateException"
        assert t.cause.message == "inner"
        assert t.cause.frames[0].location == "Inner.java:20"

    def test_frames_after_cause_go_to_cause(self) -> None:
        text = (
            "java.lang.Exception: a\n"
            "\tat com.example.A.a(A.java:1)\n"
            "Caused by: java.lang.Exception: b\n"
            "\tat com.example.B.b(B.java:2)\n"
            "Caused by: java.lang.Exception: c\n"
            "\tat com.example.C.c(C.java:3)\n"
            "\t... 5 more\n"
        )
        t = parse_stack_traces(text)[0]
        assert t.cause is not None and t.cause.exception_type.endswith("Exception")
        assert t.cause.message == "b"
        assert t.cause.cause is not None and t.cause.cause.message == "c"
        assert len(t.cause.cause.frames) == 1  # "... N more" 不算帧


class TestMavenPrefix:
    def test_error_prefixed_lines(self) -> None:
        text = (
            "[ERROR] java.lang.AssertionError: expected: <2> but was: <1>\n"
            "[ERROR] \tat com.example.T.m(T.java:5)\n"
        )
        traces = parse_stack_traces(text)
        assert len(traces) == 1
        assert traces[0].simple_type == "AssertionError"
        assert traces[0].frames[0].location == "T.java:5"

    def test_junit5_sample(self) -> None:
        traces = parse_stack_traces(JUNIT5_ASSERTION_FAILURE)
        assert len(traces) == 1
        assert traces[0].exception_type == "java.lang.AssertionError"

    def test_junit4_sample_with_caused_by(self) -> None:
        traces = parse_stack_traces(JUNIT4_NPE_ERROR)
        assert len(traces) == 1
        t = traces[0]
        assert t.simple_type == "NullPointerException"
        assert "user" in (t.message or "")
        assert any(f.location == "OrderService.java:82" for f in t.frames)
        assert t.cause is not None
        assert t.cause.simple_type == "IllegalStateException"

    def test_garbage_has_no_traces(self) -> None:
        assert parse_stack_traces("everything is fine\n[INFO] BUILD SUCCESS") == []
