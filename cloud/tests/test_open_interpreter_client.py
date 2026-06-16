import unittest
from unittest import mock

import src.open_interpreter_client as oi


class OpenInterpreterClientTests(unittest.TestCase):
    def test_summarize_console_chunk_compacts_and_truncates(self) -> None:
        summary = oi.summarize_console_chunk("  line 1\n\nline   2  ", limit=20)
        self.assertEqual(summary, "line 1 line 2")

        long_summary = oi.summarize_console_chunk("a" * 20, limit=8)
        self.assertEqual(long_summary, "aaaaaaaa...")

    def test_format_open_interpreter_result_empty_output(self) -> None:
        self.assertEqual(
            oi.format_open_interpreter_result("print('x')", "   "),
            "执行完成，但没有产生控制台输出。",
        )

    def test_format_open_interpreter_result_side_effect_boolean(self) -> None:
        result = oi.format_open_interpreter_result("import webbrowser\nwebbrowser.open('https://example.com')", "True")
        self.assertEqual(result, "执行完成，动作已触发。原始返回值：True")

    def test_format_open_interpreter_result_returns_normalized_output(self) -> None:
        result = oi.format_open_interpreter_result("print('ok')", "ok\r\n")
        self.assertEqual(result, "ok")

    def test_open_interpreter_config_helpers(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "OPEN_INTERPRETER_WS_URL": " ws://example.test/ ",
                "OPEN_INTERPRETER_TIMEOUT": "bad",
                "OPEN_INTERPRETER_AUTH_KEY": " secret ",
            },
            clear=False,
        ):
            self.assertEqual(oi.get_open_interpreter_ws_url(), "ws://example.test")
            self.assertEqual(oi.get_open_interpreter_timeout(), 90)
            self.assertEqual(oi.get_open_interpreter_auth_key(), "secret")
            self.assertTrue(oi.open_interpreter_is_configured())

    def test_open_interpreter_requires_auth_key(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "OPEN_INTERPRETER_WS_URL": "ws://example.test",
                "OPEN_INTERPRETER_AUTH_KEY": "",
            },
            clear=False,
        ):
            self.assertFalse(oi.open_interpreter_is_configured())
            self.assertEqual(
                oi.run_open_interpreter("print('ok')"),
                "Open Interpreter auth key is not configured. Set OPEN_INTERPRETER_AUTH_KEY.",
            )


if __name__ == "__main__":
    unittest.main()
