from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pegasus_game_manager.desktop import (
    desktop_candidate,
    discover_desktop_games,
    parse_desktop_entry,
    rpcs3_title_id,
    steam_app_id,
    xdg_desktop_dir,
)


class DesktopDiscoveryTests(unittest.TestCase):
    def _write_shortcut(self, root: Path, name: str, exec_line: str, categories: str = "Game;") -> Path:
        path = root / f"{name}.desktop"
        path.write_text(
            f"[Desktop Entry]\nType=Application\nName={name}\nExec={exec_line}\nCategories={categories}\n",
            encoding="utf-8",
        )
        return path

    def test_parse_desktop_entry_only_reads_main_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "game.desktop"
            path.write_text(
                "[Desktop Entry]\nName=Main Game\nExec=steam steam://rungameid/42\n"
                "[Desktop Action Store]\nName=Wrong Name\n",
                encoding="utf-8",
            )
            parsed = parse_desktop_entry(path)
            self.assertEqual(parsed["Name"], "Main Game")
            self.assertEqual(parsed["Exec"], "steam steam://rungameid/42")

    def test_discovery_skips_frontends_and_existing_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            desktop = Path(temp_dir)
            existing = self._write_shortcut(desktop, "Existing", "steam steam://rungameid/10")
            self._write_shortcut(desktop, "Sekiro", "steam steam://rungameid/11675818931502710784")
            self._write_shortcut(desktop, "Steam", "/usr/bin/steam %U", "Network;Game;")
            self._write_shortcut(desktop, "Pegasus Game Manager", "/tmp/pegasus-game-manager-launch")

            candidates = discover_desktop_games(
                [{"title": "Existing", "files": [str(existing)]}], desktop
            )
            self.assertEqual([item["record"]["title"] for item in candidates], ["Sekiro"])
            self.assertEqual(candidates[0]["platform"], "Steam")
            self.assertIsNone(candidates[0]["steam_app_id"])

    def test_rpcs3_shortcut_is_classified_and_uses_local_title_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write_shortcut(
                Path(temp_dir),
                "inFamous",
                '"/tmp/rpcs3.AppImage" --no-gui "%%RPCS3_GAMEID%%:BCUS98119"',
            )
            candidate = desktop_candidate(path)
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["rpcs3_title_id"], "BCUS98119")
            self.assertEqual(candidate["record"]["collection"], "RPCS3 — PlayStation 3")

    def test_extracts_only_real_steam_store_ids(self) -> None:
        self.assertEqual(steam_app_id("steam steam://rungameid/814380"), 814380)
        self.assertIsNone(steam_app_id("steam steam://rungameid/11675818931502710784"))
        self.assertEqual(rpcs3_title_id("%%RPCS3_GAMEID%%:BCUS98119"), "BCUS98119")

    def test_xdg_desktop_dir_expands_home_without_evaluating_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            config = home / "user-dirs.dirs"
            config.write_text('XDG_DESKTOP_DIR="$HOME/My Desktop"\n', encoding="utf-8")
            self.assertEqual(xdg_desktop_dir(home, config), home / "My Desktop")


if __name__ == "__main__":
    unittest.main()
