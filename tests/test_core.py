from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pegasus_game_manager.core as core
from pegasus_game_manager.core import (
    parse_metadata_file,
    render_metadata,
    validate_record,
)


class MetadataTests(unittest.TestCase):
    def test_parse_and_render_preserves_tags_and_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "metadata.pegasus.txt"
            path.write_text(
                """collection: RPCS3 — PlayStation 3
shortname: rpcs3

game: inFAMOUS
file: /home/acm/Desktop/inFamous.desktop
launch: /home/acm/.local/bin/rpcs3-launch-infamous
tag: Local
tag: RPCS3
assets.box_front: /tmp/front.png
assets.background: /tmp/background.png
""",
                encoding="utf-8",
            )
            parsed = parse_metadata_file(path)

            self.assertEqual(len(parsed["entries"]), 1)
            record = parsed["entries"][0]
            self.assertEqual(record["title"], "inFAMOUS")
            self.assertEqual(record["tags"], ["Local", "RPCS3"])
            self.assertEqual(record["artwork_box_front"], "/tmp/front.png")
            self.assertEqual(record["launch"], "/home/acm/.local/bin/rpcs3-launch-infamous")
            rendered = render_metadata(parsed["entries"], parsed["collections"])
            self.assertIn("tag: RPCS3", rendered)
            self.assertIn("assets.background: /tmp/background.png", rendered)

    def test_invalid_record_reports_missing_launch(self) -> None:
        record = {
            "title": "Example",
            "collection": "PC games",
            "files": ["/tmp/does-not-exist.game"],
            "launch_mode": "command",
            "launch": "",
        }
        errors = validate_record(record)
        self.assertTrue(any("does not exist" in error for error in errors))
        self.assertTrue(any("launch command" in error for error in errors))

    def test_cloud_collection_is_reserved_for_automatic_sync(self) -> None:
        record = {
            "title": "Example",
            "collection": "Xbox Cloud Gaming",
            "files": ["https://example.invalid/game"],
            "launch_mode": "command",
            "launch": "example-launch",
        }
        self.assertTrue(any("automatic sync" in error for error in validate_record(record)))

    def test_unknown_artwork_fields_survive_normalisation(self) -> None:
        record = core.normalise_record(
            {
                "title": "Example",
                "extras": {
                    "assets.logo": "/tmp/logo.png",
                    "x-custom": "value",
                    "unsupported": "discarded",
                },
            }
        )
        self.assertEqual(record["extras"]["assets.logo"], "/tmp/logo.png")
        self.assertEqual(record["extras"]["x-custom"], "value")
        self.assertNotIn("unsupported", record["extras"])

    def test_desktop_entry_writes_host_launcher_and_metadata(self) -> None:
        old_collection_files = core.MANAGED_COLLECTION_FILES
        old_managed_path = core.MANAGED_METADATA_PATH
        old_backup_dir = core.BACKUP_DIR
        old_launcher_dir = core.LAUNCHER_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            metadata_path = root / "managed.metadata.pegasus.txt"
            launcher_dir = root / "launchers"
            shortcut = root / "My Game.desktop"
            shortcut.write_text("[Desktop Entry]\nType=Application\nName=My Game\n", encoding="utf-8")
            core.MANAGED_COLLECTION_FILES = {"PC games": metadata_path}
            core.MANAGED_METADATA_PATH = root / "other.metadata.pegasus.txt"
            core.BACKUP_DIR = root / "backups"
            core.LAUNCHER_DIR = launcher_dir
            record = {
                "id": "my-game-test",
                "title": "My Game",
                "collection": "PC games",
                "files": [str(shortcut)],
                "launch_mode": "desktop",
                "launch": "",
            }
            catalog = {"entries": [record], "collection_defaults": {}}
            try:
                core.write_pegasus_files(catalog)
                launcher = launcher_dir / "my-game-test.sh"
                self.assertTrue(launcher.is_file())
                self.assertTrue(launcher.stat().st_mode & 0o111)
                self.assertIn("gio launch", launcher.read_text(encoding="utf-8"))
                self.assertIn("game: My Game", metadata_path.read_text(encoding="utf-8"))
                self.assertIn(f"launch: {launcher}", metadata_path.read_text(encoding="utf-8"))
            finally:
                core.MANAGED_COLLECTION_FILES = old_collection_files
                core.MANAGED_METADATA_PATH = old_managed_path
                core.BACKUP_DIR = old_backup_dir
                core.LAUNCHER_DIR = old_launcher_dir


if __name__ == "__main__":
    unittest.main()
