from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

from dashboard_server import DashboardActionError, DashboardServer, archive_dashboard_task, resolve_attachment_image
from health_run import process_alive
from loopdb import connect, initialize_schema, insert_task, now_shanghai


class AttachmentImageTests(unittest.TestCase):
    def test_dashboard_server_disallows_shared_listener(self) -> None:
        self.assertFalse(DashboardServer.allow_reuse_address)

    def test_current_process_is_detected_as_alive(self) -> None:
        self.assertTrue(process_alive(os.getpid()))
        self.assertFalse(process_alive(2_147_483_647))

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temporary.name)
        self.db_path = self.base_dir / "test.sqlite3"
        self.database = connect(self.db_path)
        initialize_schema(self.database)

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def add_task(self, task_id: str, attachment_path: str) -> None:
        insert_task(
            self.database,
            {
                "id": task_id,
                "title": task_id,
                "description": "attachment test",
                "status": "PENDING",
                "priority": "medium",
                "runtime_environment": "codex_automation",
                "created_at": now_shanghai(),
                "scope": ["project/file.txt"],
                "acceptance": ["test"],
                "attachments": [{"path": attachment_path, "role": "source"}],
            },
            actor="test",
            project_paths=["project"],
        )

    def test_registered_image_inside_task_directory_is_resolved(self) -> None:
        attachment_path = "assets/IMAGE-TASK/reference.png"
        self.add_task("IMAGE-TASK", attachment_path)
        image_path = self.base_dir / attachment_path
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(b"png")

        resolved, content_type = resolve_attachment_image(
            self.database,
            "IMAGE-TASK",
            attachment_path,
            self.base_dir,
        )

        self.assertEqual(resolved, image_path.resolve())
        self.assertEqual(content_type, "image/png")

    def test_unregistered_path_is_rejected(self) -> None:
        self.add_task("IMAGE-TASK", "assets/IMAGE-TASK/reference.png")
        with self.assertRaises(FileNotFoundError):
            resolve_attachment_image(
                self.database,
                "IMAGE-TASK",
                "assets/IMAGE-TASK/other.png",
                self.base_dir,
            )

    def test_registered_path_outside_task_directory_is_rejected(self) -> None:
        attachment_path = "assets/OTHER-TASK/reference.png"
        self.add_task("IMAGE-TASK", attachment_path)
        with self.assertRaises(PermissionError):
            resolve_attachment_image(
                self.database,
                "IMAGE-TASK",
                attachment_path,
                self.base_dir,
            )

    def test_registered_non_image_is_rejected(self) -> None:
        attachment_path = "assets/IMAGE-TASK/notes.txt"
        self.add_task("IMAGE-TASK", attachment_path)
        file_path = self.base_dir / attachment_path
        file_path.parent.mkdir(parents=True)
        file_path.write_text("notes", encoding="utf-8")
        with self.assertRaises(ValueError):
            resolve_attachment_image(
                self.database,
                "IMAGE-TASK",
                attachment_path,
                self.base_dir,
            )

    def set_status(self, task_id: str, status: str) -> int:
        self.database.execute(
            "UPDATE tasks SET status=?, row_version=row_version+1 WHERE id=?",
            (status, task_id),
        )
        return self.database.execute("SELECT row_version FROM tasks WHERE id=?", (task_id,)).fetchone()[0]

    def test_dashboard_archive_confirms_succeeded_before_archiving(self) -> None:
        self.add_task("ARCHIVE-SUCCEEDED", "assets/ARCHIVE-SUCCEEDED/reference.png")
        row_version = self.set_status("ARCHIVE-SUCCEEDED", "SUCCEEDED")

        result = archive_dashboard_task(self.db_path, "ARCHIVE-SUCCEEDED", "archive", row_version)

        self.assertTrue(result["ok"])
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["outcome"], "ARCHIVED")
        task = self.database.execute(
            "SELECT status, archived_at, row_version FROM tasks WHERE id='ARCHIVE-SUCCEEDED'"
        ).fetchone()
        history = self.database.execute(
            "SELECT from_status, to_status, actor FROM task_history "
            "WHERE task_id='ARCHIVE-SUCCEEDED' ORDER BY id DESC LIMIT 2"
        ).fetchall()
        self.assertEqual(task["status"], "CONFIRMED")
        self.assertIsNotNone(task["archived_at"])
        self.assertEqual(task["row_version"], row_version + 2)
        self.assertEqual(
            [(row["from_status"], row["to_status"], row["actor"]) for row in reversed(history)],
            [
                ("SUCCEEDED", "CONFIRMED", "human-review"),
                ("CONFIRMED", "CONFIRMED", "task-manager"),
            ],
        )

    def test_dashboard_archive_preserves_existing_terminal_status(self) -> None:
        self.add_task("ARCHIVE-FAILED", "assets/ARCHIVE-FAILED/reference.png")
        row_version = self.set_status("ARCHIVE-FAILED", "FAILED")
        self.database.execute(
            "UPDATE tasks SET result_summary='keep summary', result_error='keep error' WHERE id='ARCHIVE-FAILED'"
        )

        result = archive_dashboard_task(self.db_path, "ARCHIVE-FAILED", "archive", row_version)

        self.assertFalse(result["confirmed"])
        task = self.database.execute(
            "SELECT status, archived_at, result_summary, result_error FROM tasks WHERE id='ARCHIVE-FAILED'"
        ).fetchone()
        self.assertEqual(task["status"], "FAILED")
        self.assertIsNotNone(task["archived_at"])
        self.assertEqual((task["result_summary"], task["result_error"]), ("keep summary", "keep error"))

    def test_dashboard_archive_rejects_illegal_status_repeat_and_stale_version(self) -> None:
        self.add_task("ARCHIVE-PENDING", "assets/ARCHIVE-PENDING/reference.png")
        pending_version = self.database.execute(
            "SELECT row_version FROM tasks WHERE id='ARCHIVE-PENDING'"
        ).fetchone()[0]
        with self.assertRaisesRegex(DashboardActionError, "不允许归档"):
            archive_dashboard_task(self.db_path, "ARCHIVE-PENDING", "archive", pending_version)

        self.add_task("ARCHIVE-REPEAT", "assets/ARCHIVE-REPEAT/reference.png")
        archived_version = self.set_status("ARCHIVE-REPEAT", "CANCELLED")
        archive_dashboard_task(self.db_path, "ARCHIVE-REPEAT", "archive", archived_version)
        with self.assertRaisesRegex(DashboardActionError, "状态已变化"):
            archive_dashboard_task(self.db_path, "ARCHIVE-REPEAT", "archive", archived_version)

        self.add_task("ARCHIVE-STALE", "assets/ARCHIVE-STALE/reference.png")
        stale_version = self.set_status("ARCHIVE-STALE", "CONFIRMED")
        self.set_status("ARCHIVE-STALE", "FAILED")
        with self.assertRaisesRegex(DashboardActionError, "状态已变化"):
            archive_dashboard_task(self.db_path, "ARCHIVE-STALE", "archive", stale_version)
        self.assertIsNone(
            self.database.execute("SELECT archived_at FROM tasks WHERE id='ARCHIVE-STALE'").fetchone()[0]
        )

    def test_dashboard_archive_validates_action_input(self) -> None:
        for task_id, action, row_version in [
            ("bad id", "archive", 1),
            ("VALID-ID", "delete", 1),
            ("VALID-ID", "archive", True),
        ]:
            with self.subTest(task_id=task_id, action=action, row_version=row_version):
                with self.assertRaises(DashboardActionError):
                    archive_dashboard_task(self.db_path, task_id, action, row_version)


if __name__ == "__main__":
    unittest.main()
