from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pegasus_game_manager.artwork as artwork


class ArtworkTests(unittest.TestCase):
    def test_exact_title_wins_over_more_popular_partial_match(self) -> None:
        match = artwork.choose_steam_match(
            "Outlast",
            [
                {"type": "app", "id": 1304930, "name": "The Outlast Trials"},
                {"type": "app", "id": 238320, "name": "Outlast"},
                {"type": "app", "id": 414700, "name": "Outlast 2"},
            ],
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], 238320)

    def test_extended_edition_name_can_match_short_desktop_title(self) -> None:
        match = artwork.choose_steam_match(
            "Sekiro",
            [{"type": "app", "id": 814380, "name": "Sekiro™: Shadows Die Twice - GOTY Edition"}],
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], 814380)

    def test_ambiguous_partial_match_is_not_accepted(self) -> None:
        match = artwork.choose_steam_match(
            "Black Flag",
            [
                {"type": "app", "id": 3751950, "name": "Assassin's Creed Black Flag Resynced"},
                {"type": "app", "id": 242050, "name": "Assassin’s Creed IV Black Flag"},
            ],
        )
        self.assertIsNone(match)

    def test_enrichment_downloads_both_artwork_roles_and_metadata(self) -> None:
        fake_match = {
            "app_id": 814380,
            "name": "Sekiro: Shadows Die Twice",
            "match_score": 0.9,
            "summary": "A difficult action adventure.",
            "genres": ["Action", "Adventure"],
            "background_url": "https://example.invalid/background.jpg",
            "header_url": "https://example.invalid/header.jpg",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            old_artwork_dir = artwork.ARTWORK_DIR
            artwork.ARTWORK_DIR = Path(temp_dir)

            def fake_download(_url: str, stem: Path, _timeout: float = 15.0) -> Path:
                target = stem.with_suffix(".jpg")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"image")
                return target

            try:
                with mock.patch.object(artwork, "find_steam_game", return_value=fake_match), mock.patch.object(
                    artwork, "download_image", side_effect=fake_download
                ):
                    result = artwork.enrich_game_record(
                        {"title": "Sekiro", "files": ["/tmp/Sekiro.desktop"]}
                    )
            finally:
                artwork.ARTWORK_DIR = old_artwork_dir

        record = result["record"]
        self.assertTrue(record["artwork_box_front"].endswith("box_front.jpg"))
        self.assertTrue(record["artwork_background"].endswith("background.jpg"))
        self.assertEqual(record["genres"], ["Action", "Adventure"])
        self.assertEqual(record["summary"], "A difficult action adventure.")
        self.assertEqual(record["extras"]["x-pegasus-artwork-source"], "Steam Store app 814380")

    def test_provider_failure_keeps_importable_record(self) -> None:
        with mock.patch.object(artwork, "find_steam_game", side_effect=OSError("offline")):
            result = artwork.enrich_game_record({"title": "Unknown", "files": ["/tmp/game.desktop"]})
        self.assertEqual(result["record"]["title"], "Unknown")
        self.assertIn("unavailable", result["status"])


if __name__ == "__main__":
    unittest.main()
