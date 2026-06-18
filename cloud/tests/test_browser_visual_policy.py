import unittest
from unittest import mock

import src.browser_visual_policy as policy


class BrowserVisualPolicyTests(unittest.TestCase):
    def test_normalise_click_action_clamps_fields(self) -> None:
        action = policy.normalise_visual_action(
            {
                "action": "click",
                "x": -20,
                "y": 1200,
                "confidence": 2,
                "wait_ms": 20000,
                "reason": "  open   comments  ",
                "target_description": " comment button ",
                "expected_change": " comments panel opens ",
            }
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action["x"], 0)
        self.assertEqual(action["y"], 1000)
        self.assertEqual(action["confidence"], 1.0)
        self.assertEqual(action["wait_ms"], 10000)
        self.assertEqual(action["reason"], "open comments")
        self.assertEqual(action["target_description"], "comment button")
        self.assertEqual(action["expected_change"], "comments panel opens")

    def test_visual_action_schema_rejects_incomplete_click(self) -> None:
        action = policy.normalise_visual_action(
            {
                "action": "click",
                "x": 200,
                "confidence": 0.9,
                "target_description": "video card",
                "expected_change": "video detail opens",
            }
        )

        self.assertIsNone(action)

    def test_visual_decision_schema_preserves_ids_and_limits_actions(self) -> None:
        decision = policy.normalise_visual_decision(
            {
                "status": "continue",
                "summary": " click a visible card ",
                "model": "vision-model",
                "actions": [
                    {
                        "action": "click",
                        "x": 100,
                        "y": 120,
                        "confidence": 0.8,
                        "target_description": "first video card",
                        "expected_change": "video detail opens",
                        "task_id": "task-1",
                        "action_id": "action-1",
                    },
                    {"action": "wait"},
                    {"action": "wait"},
                ],
            }
        )

        self.assertEqual(decision["status"], "continue")
        self.assertEqual(decision["summary"], "click a visible card")
        self.assertEqual(decision["model"], "vision-model")
        self.assertEqual(len(decision["actions"]), 2)
        self.assertEqual(decision["actions"][0]["task_id"], "task-1")
        self.assertEqual(decision["actions"][0]["action_id"], "action-1")

    def test_low_confidence_action_is_blocked(self) -> None:
        action = {
            "action": "click",
            "confidence": 0.44,
            "target_description": "visible button",
            "expected_change": "panel opens",
        }
        with mock.patch.object(policy, "BROWSER_VISION_ACTION_MIN_CONFIDENCE", 0.55):
            issue = policy.visual_action_safety_issue(action)

        self.assertEqual(issue, "low confidence 0.44 < 0.55")

    def test_click_requires_target_and_expected_change(self) -> None:
        base_action = {"action": "click", "confidence": 0.9}

        self.assertEqual(policy.visual_action_safety_issue(base_action), "missing target_description")

        with_target = {**base_action, "target_description": "login button"}
        self.assertEqual(policy.visual_action_safety_issue(with_target), "missing expected_change")

    def test_normalise_verification_defaults_and_clamps(self) -> None:
        verification = policy.normalise_visual_verification(
            {
                "status": "unknown",
                "changed": True,
                "confidence": 9,
                "risk": "surprise",
                "observed_change": "  dialog   opened ",
            }
        )

        self.assertEqual(verification["status"], "continue")
        self.assertTrue(verification["changed"])
        self.assertTrue(verification["matched_expected_change"])
        self.assertEqual(verification["confidence"], 1.0)
        self.assertEqual(verification["risk"], "none")
        self.assertEqual(verification["observed_change"], "dialog opened")

    def test_verification_policy_retries_low_confidence_then_stops(self) -> None:
        verification = {
            "status": "continue",
            "changed": True,
            "matched_expected_change": True,
            "confidence": 0.2,
            "risk": "none",
            "misclick": False,
        }
        action = {"action": "click", "expected_change": "comments panel opens"}

        with (
            mock.patch.object(policy, "BROWSER_VISION_VERIFY_MIN_CONFIDENCE", 0.45),
            mock.patch.object(policy, "BROWSER_VISION_VERIFY_MAX_RETRIES", 2),
        ):
            first_status, first_reason = policy.classify_visual_verification(
                verification,
                action,
                retry_count=1,
            )
            second_status, second_reason = policy.classify_visual_verification(
                verification,
                action,
                retry_count=2,
            )

        self.assertEqual(first_status, "retry")
        self.assertIn("confidence 0.20 < 0.45", first_reason)
        self.assertEqual(second_status, "need_user")
        self.assertIn("Verification retry limit reached", second_reason)

    def test_verification_policy_retries_when_expected_change_not_matched(self) -> None:
        verification = {
            "status": "continue",
            "changed": True,
            "matched_expected_change": False,
            "confidence": 0.9,
            "risk": "none",
            "misclick": False,
        }
        action = {"action": "click", "expected_change": "comments panel opens"}

        status, reason = policy.classify_visual_verification(verification, action, retry_count=1)

        self.assertEqual(status, "retry")
        self.assertIn("expected change not matched", reason)

    def test_verification_policy_stops_on_high_risk_or_misclick(self) -> None:
        action = {"action": "click", "expected_change": "safe panel opens"}

        high_risk_status, high_risk_reason = policy.classify_visual_verification(
            {
                "status": "continue",
                "changed": True,
                "matched_expected_change": True,
                "confidence": 0.9,
                "risk": "high",
                "misclick": False,
            },
            action,
            retry_count=1,
        )
        misclick_status, misclick_reason = policy.classify_visual_verification(
            {
                "status": "continue",
                "changed": True,
                "matched_expected_change": True,
                "confidence": 0.9,
                "risk": "none",
                "misclick": True,
            },
            action,
            retry_count=1,
        )

        self.assertEqual(high_risk_status, "need_user")
        self.assertIn("high-risk", high_risk_reason)
        self.assertEqual(misclick_status, "need_user")
        self.assertIn("misclick", misclick_reason)

    def test_failure_classifier_names_common_browser_failures(self) -> None:
        low_confidence = policy.classify_visual_failure(
            "action_safety",
            "low confidence 0.40 < 0.55",
            status="continue",
            retry_count=1,
        )
        click_mismatch = policy.classify_visual_failure(
            "click_preflight",
            "hit-test target mismatch score 0.00 < 0.16",
            status="continue",
            retry_count=1,
        )
        retry_limit = policy.classify_visual_failure(
            "verification",
            "Verification retry limit reached: expected change not matched",
            status="need_user",
            retry_count=2,
        )

        self.assertEqual(low_confidence["category"], "action_low_confidence")
        self.assertEqual(low_confidence["failure_type"], "action_low_confidence")
        self.assertTrue(low_confidence["recoverable"])
        self.assertEqual(click_mismatch["category"], "click_target_mismatch")
        self.assertEqual(retry_limit["category"], "verification_retry_limit")
        self.assertEqual(retry_limit["severity"], "error")
        self.assertFalse(retry_limit["recoverable"])

    def test_failure_classifier_standardizes_unknown_failure_type(self) -> None:
        failure = policy.classify_visual_failure(
            "Some New Source!",
            "example",
            status="failed",
        )

        self.assertEqual(failure["failure_type"], "some_new_source")
        self.assertEqual(failure["category"], failure["failure_type"])

    def test_failure_classifier_detects_contextual_browser_failures(self) -> None:
        login_wall = policy.classify_visual_failure(
            "click_preflight",
            "hit-test target mismatch score 0.00 < 0.16",
            context={"hit_test": {"target": {"text": "请先登录后继续", "role": "dialog"}}},
        )
        overlay = policy.classify_visual_failure(
            "click_preflight",
            "hit-test target mismatch score 0.00 < 0.16",
            context={"hit_test": {"target": {"className": "modal overlay mask", "text": "Allow notifications"}}},
        )
        viewport = policy.classify_visual_failure(
            "click_preflight",
            "hit-test target mismatch score 0.00 < 0.16",
            context={
                "screenshot": {"viewport": {"width": 1280, "height": 720}},
                "hit_test": {"viewport": {"width": 980, "height": 720}, "target": {"text": "Open"}},
            },
        )
        refreshed = policy.classify_visual_failure(
            "verification",
            "expected change not matched",
            context={
                "action": {"expected_change": "comments panel opens"},
                "action_result": {"result": {"target": {"beforeUrl": "https://example.test/a", "afterUrl": "https://example.test/b"}}},
            },
        )
        dpr = policy.classify_visual_failure(
            "verification",
            "expected change not matched",
            context={
                "before": {"device_pixel_ratio": 1.0},
                "after": {"device_pixel_ratio": 1.25},
            },
        )

        self.assertEqual(login_wall["failure_type"], "login_wall_blocking")
        self.assertEqual(overlay["failure_type"], "overlay_blocking_target")
        self.assertEqual(viewport["failure_type"], "viewport_mismatch")
        self.assertEqual(refreshed["failure_type"], "page_refreshed_unexpectedly")
        self.assertEqual(refreshed["severity"], "error")
        self.assertEqual(dpr["failure_type"], "dpr_coordinate_mismatch")


if __name__ == "__main__":
    unittest.main()
