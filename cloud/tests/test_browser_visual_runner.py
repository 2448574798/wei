import unittest
from unittest import mock

import src.browser_visual_runner as runner


class BrowserVisualRunnerTests(unittest.TestCase):
    def test_extract_json_object_handles_markdown_wrapped_json(self) -> None:
        parsed = runner._extract_json_object('```json\n{"status":"done","summary":"ok"}\n```')

        self.assertEqual(parsed["status"], "done")
        self.assertEqual(parsed["summary"], "ok")

    def test_diagnostics_formatter_summarizes_controls_and_text(self) -> None:
        lines = runner.format_browser_diagnostics(
            {
                "diagnostics": {
                    "ok": True,
                    "viewport": {"width": 1280, "height": 720},
                    "scroll": {"y": 300, "height": 2000},
                    "readyState": "complete",
                    "headings": ["Search results", ""],
                    "controls": [
                        {
                            "tag": "button",
                            "role": "button",
                            "text": "Open filters",
                            "selector": "button.filters",
                        },
                        {
                            "tag": "a",
                            "href": "https://example.test",
                            "selector": "a.result",
                        },
                    ],
                    "controlCount": 3,
                    "visibleText": "A useful result is visible.",
                }
            },
            max_controls=1,
        )

        joined = "\n".join(lines)
        self.assertIn("viewport=1280x720", joined)
        self.assertIn("Visible headings: Search results", joined)
        self.assertIn("Open filters", joined)
        self.assertIn("2 more visible controls omitted", joined)
        self.assertIn("A useful result is visible.", joined)

    def test_compact_visual_screenshot_keeps_small_trace_image(self) -> None:
        with (
            mock.patch.object(runner, "BROWSER_VISUAL_TRACE_SCREENSHOTS", True),
            mock.patch.object(runner, "BROWSER_VISUAL_TRACE_MAX_IMAGE_CHARS", 20),
        ):
            compact = runner.compact_visual_screenshot(
                {
                    "title": "Example",
                    "url": "https://example.test",
                    "mime_type": "image/png",
                    "image_base64": "abc123",
                    "image_base64_length": 6,
                }
            )

        self.assertEqual(compact["title"], "Example")
        self.assertEqual(compact["image_base64"], "abc123")

    def test_compact_visual_screenshot_omits_large_trace_image(self) -> None:
        with (
            mock.patch.object(runner, "BROWSER_VISUAL_TRACE_SCREENSHOTS", True),
            mock.patch.object(runner, "BROWSER_VISUAL_TRACE_MAX_IMAGE_CHARS", 4),
        ):
            compact = runner.compact_visual_screenshot(
                {
                    "image_base64": "abc123",
                    "image_base64_length": 6,
                }
            )

        self.assertNotIn("image_base64", compact)
        self.assertIn("exceeds", compact["image_omitted_reason"])

    def test_click_hit_test_scores_target_text_overlap(self) -> None:
        score = runner.click_hit_test_match_score(
            "Open filters button",
            {
                "ok": True,
                "actionable": True,
                "target": {
                    "tag": "button",
                    "text": "Open filters",
                    "selector": "button.filters",
                },
            },
        )

        self.assertGreaterEqual(score, 0.6)

    def test_click_hit_test_blocks_low_confidence_mismatch(self) -> None:
        action = {
            "action": "click",
            "confidence": 0.6,
            "target_description": "Open filters button",
        }
        hit_test = {
            "ok": True,
            "actionable": True,
            "target": {
                "tag": "button",
                "text": "Delete account",
                "selector": "button.danger",
            },
        }

        with (
            mock.patch.object(runner, "BROWSER_VISUAL_CLICK_PREFLIGHT_ENABLED", True),
            mock.patch.object(runner, "BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE", 0.16),
        ):
            issue, score = runner.click_hit_test_safety_issue(action, hit_test)

        self.assertLess(score, 0.16)
        self.assertIn("target mismatch", issue)

    def test_click_hit_test_allows_high_confidence_mismatch_for_verifier(self) -> None:
        action = {
            "action": "click",
            "confidence": 0.92,
            "target_description": "Open filters button",
        }
        hit_test = {
            "ok": True,
            "actionable": True,
            "target": {
                "tag": "button",
                "text": "Filter",
                "selector": "button[aria-label=\"filter\"]",
            },
        }

        with (
            mock.patch.object(runner, "BROWSER_VISUAL_CLICK_PREFLIGHT_ENABLED", True),
            mock.patch.object(runner, "BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE", 0.9),
        ):
            issue, _score = runner.click_hit_test_safety_issue(action, hit_test)

        self.assertEqual(issue, "")

    def test_click_hit_test_allows_generic_target_description(self) -> None:
        action = {
            "action": "click",
            "confidence": 0.6,
            "target_description": "click button",
        }
        hit_test = {
            "ok": True,
            "actionable": True,
            "target": {
                "tag": "button",
                "text": "Continue",
                "selector": "button.continue",
            },
        }

        with (
            mock.patch.object(runner, "BROWSER_VISUAL_CLICK_PREFLIGHT_ENABLED", True),
            mock.patch.object(runner, "BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE", 0.9),
        ):
            issue, _score = runner.click_hit_test_safety_issue(action, hit_test)

        self.assertEqual(issue, "")

    def test_click_hit_test_allows_video_card_metadata_hit(self) -> None:
        action = {
            "action": "click",
            "confidence": 0.72,
            "target_description": "first video card with title 地球之肺亚马逊雨林",
        }
        hit_test = {
            "ok": True,
            "actionable": False,
            "target": {
                "tag": "div",
                "text": "05:13 1.5万",
                "selector": "div.t73oY2Aa.Fiub_RNJ > div.Ua9Qmm8U > div.RnRWkAg2",
                "className": "RnRWkAg2",
            },
            "ancestors": [
                {
                    "tag": "div",
                    "className": "waterfall-videoCardContainer jingxuanVideoCard",
                    "selector": "div.waterfall-videoCardContainer.jingxuanVideoCard",
                }
            ],
        }

        with (
            mock.patch.object(runner, "BROWSER_VISUAL_CLICK_PREFLIGHT_ENABLED", True),
            mock.patch.object(runner, "BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE", 0.16),
        ):
            issue, score = runner.click_hit_test_safety_issue(action, hit_test)

        self.assertEqual(issue, "")
        self.assertGreaterEqual(score, 0.16)

    def test_visual_operation_records_model_no_action_failure(self) -> None:
        artifacts = []
        screenshot = {
            "title": "Example",
            "url": "https://example.test",
            "mime_type": "image/png",
            "image_base64": "abc123",
            "image_base64_length": 6,
        }

        with (
            mock.patch.object(runner, "_browser_worker_request", return_value=screenshot),
            mock.patch.object(
                runner,
                "_call_browser_vision_model",
                return_value={"status": "continue", "summary": "not sure", "actions": []},
            ),
        ):
            output = runner.run_local_browser_visual_operation(
                instruction="open comments",
                rounds=1,
                artifact_callback=artifacts.append,
            )

        final = artifacts[-1]
        self.assertIn("Last failure: category=model_no_action", output)
        self.assertEqual(final["event"], "final")
        self.assertEqual(final["last_failure"]["category"], "model_no_action")
        self.assertEqual(final["failures"][-1]["category"], "model_no_action")

    def test_visual_operation_records_action_execution_failure(self) -> None:
        artifacts = []
        screenshot = {
            "title": "Example",
            "url": "https://example.test",
            "mime_type": "image/png",
            "image_base64": "abc123",
            "image_base64_length": 6,
        }

        with (
            mock.patch.object(runner, "_browser_worker_request", return_value=screenshot),
            mock.patch.object(
                runner,
                "_call_browser_vision_model",
                return_value={
                    "status": "continue",
                    "summary": "press key",
                    "actions": [
                        {
                            "action": "press",
                            "key": "Enter",
                            "reason": "submit focused form",
                            "target_description": "focused form",
                            "expected_change": "form submits",
                            "confidence": 0.9,
                        }
                    ],
                },
            ),
            mock.patch.object(runner, "_run_visual_action", side_effect=RuntimeError("MCP crashed")),
        ):
            output = runner.run_local_browser_visual_operation(
                instruction="submit form",
                rounds=1,
                artifact_callback=artifacts.append,
            )

        final = artifacts[-1]
        self.assertIn("Final status: failed", output)
        self.assertEqual(final["last_failure"]["category"], "action_execution_failed")
        self.assertEqual(final["last_failure"]["severity"], "error")


if __name__ == "__main__":
    unittest.main()
