from __future__ import annotations

import copy
import json
import re
import tempfile
import unicodedata
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .core import ARTWORK_DIR, HOME, normalise_record, slugify


USER_AGENT = "PegasusGameManager/0.2 (+local Pegasus metadata manager)"
STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
STORE_DETAILS_URL = "https://store.steampowered.com/api/appdetails"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
_EDITION_WORDS = {
    "edition",
    "goty",
    "game",
    "of",
    "the",
    "year",
    "deluxe",
    "ultimate",
    "complete",
}


def normalise_game_title(value: str) -> str:
    value = value.replace("™", "").replace("®", "").replace("©", "")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = re.sub(r"[^a-zA-Z0-9]+", " ", value).strip().casefold()
    return " ".join(value.split())


def title_match_score(query: str, candidate: str) -> float:
    query_norm = normalise_game_title(query)
    candidate_norm = normalise_game_title(candidate)
    if not query_norm or not candidate_norm:
        return 0.0
    if query_norm == candidate_norm:
        return 1.0

    query_tokens = set(query_norm.split())
    candidate_tokens = set(candidate_norm.split())
    meaningful_candidate = candidate_tokens - _EDITION_WORDS
    overlap = query_tokens & candidate_tokens
    coverage = len(overlap) / len(query_tokens)
    precision = len(overlap) / max(1, len(meaningful_candidate))
    sequence = SequenceMatcher(None, query_norm, candidate_norm).ratio()
    score = 0.5 * coverage + 0.2 * precision + 0.3 * sequence
    if candidate_norm.startswith(query_norm + " "):
        score = max(score, 0.88)
    elif f" {query_norm} " in f" {candidate_norm} ":
        score = max(score, 0.82)
    return min(score, 1.0)


def choose_steam_match(query: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for item in items:
        if item.get("type") not in {None, "app"} or not item.get("id") or not item.get("name"):
            continue
        score = title_match_score(query, str(item["name"]))
        ranked.append((score, item))
    if not ranked:
        return None
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    if best_score < 0.78:
        return None
    if best_score < 0.99 and second_score >= best_score - 0.06:
        return None
    result = dict(best)
    result["match_score"] = round(best_score, 3)
    return result


def _read_json(url: str, timeout: float = 12.0) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def find_steam_game(title: str, app_id: int | None = None) -> dict[str, Any] | None:
    matched_title = title
    score = 1.0 if app_id is not None else 0.0
    if app_id is None:
        query = urllib.parse.urlencode({"term": title, "cc": "GB", "l": "english"})
        search = _read_json(f"{STORE_SEARCH_URL}?{query}")
        if not isinstance(search, dict):
            return None
        match = choose_steam_match(title, list(search.get("items", [])))
        if match is None:
            return None
        app_id = int(match["id"])
        matched_title = str(match["name"])
        score = float(match["match_score"])

    query = urllib.parse.urlencode({"appids": app_id, "cc": "GB", "l": "english"})
    payload = _read_json(f"{STORE_DETAILS_URL}?{query}")
    wrapped = payload.get(str(app_id), {}) if isinstance(payload, dict) else {}
    if not wrapped.get("success") or not isinstance(wrapped.get("data"), dict):
        return None
    details = dict(wrapped["data"])
    return {
        "app_id": app_id,
        "name": str(details.get("name") or matched_title),
        "match_score": score,
        "summary": str(details.get("short_description") or "").strip(),
        "genres": [
            str(item.get("description", "")).strip()
            for item in details.get("genres", [])
            if isinstance(item, dict) and str(item.get("description", "")).strip()
        ],
        "background_url": str(details.get("background_raw") or details.get("background") or details.get("header_image") or ""),
        "header_url": str(details.get("header_image") or ""),
    }


def _image_extension(content_type: str, url: str) -> str:
    content_type = content_type.partition(";")[0].strip().casefold()
    known = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/avif": ".avif",
    }
    if content_type in known:
        return known[content_type]
    suffix = Path(urllib.parse.urlparse(url).path).suffix.casefold()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif"} else ".jpg"


def download_image(url: str, destination_stem: Path, timeout: float = 15.0) -> Path:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/png,image/jpeg"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        if content_type and not content_type.casefold().startswith("image/"):
            raise ValueError(f"Artwork URL returned {content_type}, not an image.")
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_IMAGE_BYTES:
            raise ValueError("Artwork image is unexpectedly large.")
        data = response.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Artwork image is empty or unexpectedly large.")

    destination = destination_stem.with_suffix(_image_extension(content_type, url))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", delete=False) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    temporary.replace(destination)
    return destination


def _local_rpcs3_artwork(title_id: str) -> tuple[str, str]:
    if not title_id:
        return "", ""
    root = HOME / ".config" / "rpcs3" / "dev_hdd0" / "game" / title_id
    cover = root / "ICON0.PNG"
    background = root / "PIC1.PNG"
    return (str(cover) if cover.is_file() else "", str(background) if background.is_file() else "")


def enrich_game_record(record: dict[str, Any], hints: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fill safe metadata and local artwork paths, returning a UI-ready result.

    Provider/network failures are deliberately returned as status text so a
    disconnected machine never prevents a shortcut from being imported.
    """

    hints = hints or {}
    enriched = normalise_record(copy.deepcopy(record))
    title_id = str(hints.get("rpcs3_title_id") or enriched["extras"].get("x-pegasus-rpcs3-title-id", ""))
    local_cover, local_background = _local_rpcs3_artwork(title_id)
    if not enriched["artwork_box_front"] and local_cover:
        enriched["artwork_box_front"] = local_cover
    if not enriched["artwork_background"] and local_background:
        enriched["artwork_background"] = local_background

    app_id_value = hints.get("steam_app_id") or enriched["extras"].get("x-pegasus-steam-app-id")
    try:
        app_id = int(app_id_value) if app_id_value else None
    except (TypeError, ValueError):
        app_id = None

    try:
        match = find_steam_game(enriched["title"], app_id)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        if enriched["artwork_box_front"] or enriched["artwork_background"]:
            return {
                "record": normalise_record(enriched),
                "status": "Using local artwork; online metadata is currently unavailable.",
                "matched_title": "",
                "error": str(exc),
            }
        return {
            "record": normalise_record(enriched),
            "status": "Artwork lookup is unavailable; the game can still be added.",
            "matched_title": "",
            "error": str(exc),
        }

    if match is None:
        if enriched["artwork_box_front"] or enriched["artwork_background"]:
            status = "Using artwork supplied by the game or emulator."
        else:
            status = "No confident artwork match found; review this game after importing."
        return {"record": normalise_record(enriched), "status": status, "matched_title": ""}

    app_id = int(match["app_id"])
    art_root = ARTWORK_DIR / f"{slugify(enriched['title'])}-{app_id}"
    errors: list[str] = []
    if not enriched["artwork_box_front"]:
        cover_urls = [
            f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{app_id}/library_600x900_2x.jpg",
            f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{app_id}/library_600x900.jpg",
            str(match.get("header_url") or ""),
        ]
        for url in cover_urls:
            if not url:
                continue
            try:
                enriched["artwork_box_front"] = str(download_image(url, art_root / "box_front"))
                break
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
    if not enriched["artwork_background"] and match.get("background_url"):
        try:
            enriched["artwork_background"] = str(
                download_image(str(match["background_url"]), art_root / "background")
            )
        except (OSError, ValueError) as exc:
            errors.append(str(exc))

    if not enriched["summary"]:
        enriched["summary"] = str(match.get("summary") or "")
    if not enriched["genres"]:
        enriched["genres"] = list(match.get("genres") or [])
    enriched["extras"]["x-pegasus-artwork-source"] = f"Steam Store app {app_id}"
    enriched["extras"]["x-pegasus-artwork-match"] = str(match["name"])

    found = bool(enriched["artwork_box_front"] or enriched["artwork_background"])
    status = f"Matched {match['name']} and downloaded artwork." if found else f"Matched {match['name']}, but its artwork could not be downloaded."
    return {
        "record": normalise_record(enriched),
        "status": status,
        "matched_title": str(match["name"]),
        "confidence": float(match.get("match_score", 0.0)),
        "error": "; ".join(errors),
    }
