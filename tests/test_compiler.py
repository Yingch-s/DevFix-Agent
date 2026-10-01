"""编译错误解析器测试。"""

from __future__ import annotations

from devfix.parsing.compiler import parse_compile_errors
from tests.sample_logs import COMPILATION_ERROR, JUNIT5_ASSERTION_FAILURE


class TestCompilationErrorLog:
    def test_two_errors_parsed(self) -> None:
        errors = parse_compile_errors(COMPILATION_ERROR)
        assert len(errors) == 2

    def test_first_error_with_symbol_and_location(self) -> None:
        e = parse_compile_errors(COMPILATION_ERROR)[0]
        # Maven 在 Windows 打印的 /D:/... 前缀应被规范化
        assert e.file_path == (
            "D:/Workspace/demo-projects/order-service/src/main/java"
            "/com/example/order/OrderController.java"
        )
        assert e.line == 31
        assert e.column == 25
        assert e.message == "cannot find symbol"
        assert e.symbol == "method findActiveById(int)"
        assert "userRepository" in (e.location or "")

    def test_second_error(self) -> None:
        e = parse_compile_errors(COMPILATION_ERROR)[1]
        assert e.file_path.endswith("OrderService.java")
        assert (e.line, e.column) == (82, 9)
        assert e.message.startswith("incompatible types")


class TestNegativeCases:
    def test_warning_lines_are_not_errors(self) -> None:
        """javac 警告行与错误行格式相同，必须按级别前缀区分（真实日志暴露的 bug）。"""
        log = (
            "[WARNING] /D:/proj/src/test/java/com/example/CharSetTest.java:[394,55] "
            "最后一个参数使用了不准确的变量类型的 varargs 方法的非 varargs 调用\n"
            "[INFO] /D:/proj/src/main/java/com/example/Other.java:[10,1] 某些输入文件使用已过时的 API\n"
            "[ERROR] /D:/proj/src/main/java/com/example/Real.java:[12,5] cannot find symbol\n"
        )
        errors = parse_compile_errors(log)
        assert len(errors) == 1
        assert errors[0].file_path.endswith("Real.java")

    def test_test_failure_log_has_no_compile_errors(self) -> None:
        assert parse_compile_errors(JUNIT5_ASSERTION_FAILURE) == []

    def test_marker_alone_is_not_an_error(self) -> None:
        # 只有 COMPILATION ERROR 标记、没有具体错误行时，不产出 CompileError
        assert parse_compile_errors("[ERROR] COMPILATION ERROR") == []
