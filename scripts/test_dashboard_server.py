from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

from dashboard_server import resolve_attachment_image
from loopdb import connect, initialize_schema, insert_task, now_shanghai


class AttachmentImageTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
