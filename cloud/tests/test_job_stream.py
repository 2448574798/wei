import unittest

from src.app import _job_stream_snapshot


class JobStreamTests(unittest.TestCase):
    def test_initial_snapshot_does_not_duplicate_existing_progress(self) -> None:
        job = {
            "id": "job-1",
            "status": "running",
            "progress": ["started", "round 1"],
            "artifacts": [{"event": "screenshot"}],
        }

        events, progress_offset, artifact_offset, completed = _job_stream_snapshot(
            job,
            progress_offset=0,
            artifact_offset=0,
        )

        self.assertEqual([event["type"] for event in events], ["job_snapshot"])
        self.assertEqual(progress_offset, 2)
        self.assertEqual(artifact_offset, 1)
        self.assertFalse(completed)

    def test_stream_snapshot_emits_new_progress_after_offset(self) -> None:
        job = {
            "id": "job-1",
            "status": "completed",
            "progress": ["started", "done"],
            "artifacts": [],
            "result": "ok",
        }

        events, progress_offset, artifact_offset, completed = _job_stream_snapshot(
            job,
            progress_offset=1,
            artifact_offset=0,
        )

        self.assertEqual([event["type"] for event in events], ["job_progress", "job_finished"])
        self.assertEqual(events[0]["payload"]["message"], "done")
        self.assertEqual(progress_offset, 2)
        self.assertEqual(artifact_offset, 0)
        self.assertTrue(completed)


if __name__ == "__main__":
    unittest.main()
