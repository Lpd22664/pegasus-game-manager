from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .core import APP_NAME, HOME, normalise_record, stable_id


_STEAM_GAME_RE = re.compile(r"steam://rungameid/(\d+)", re.IGNORECASE)
_RPCS3_GAME_RE = re.compile(r"(?:RPCS3_GAMEID%%:|--game-id[= ]|/game/)([A-Z]{4}\d{5})", re.IGNORECASE)
_GAME_EXEC_PATTERNS = (
    "steam://rungameid/",
    "heroic://launch/",
    "lutris:rungame/",
    "lutris:rungameid/",
    "com.usebottles.bottles -b ",
    "rpcs3_gameid",
)
_FRONTEND_NAMES = {
    "steam",
    "lutris",
    "heroic games launcher",
    "pegasus",
    "pegasus frontend",
    APP_NAME.casefold(),
    "retroarch",
    "rpcs3",
    "dolphin emulator",
}
_FRONTEND_EXECUTABLES = {
    "steam",
    "steam-runtime",
    "lutris",
    "heroic",
    "pegasus-fe",
    "retroarch",
    "rpcs3",
    "dolphin-emu",
}


def _unescape_desktop_value(value: str) -> str:
    replacements = {"\\s": " ", "\\n": "\n", "\\t": "\t", "\\r": "\r", "\\\\": "\\"}
    for escaped, replacement in replacements.items():
        value = value.replace(escaped, replacement)
    return value.strip()


def parse_desktop_entry(path: Path) -> dict[str, str]:
    """Read the main Desktop Entry group without executing or expanding it."""

    values: dict[str, str] = {}
    section = ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != "Desktop Entry" or "=" not in raw_line:
            continue
        key, _, value = raw_line.partition("=")
        key = key.strip()
        if key and key not in values:
            values[key] = _unescape_desktop_value(value)

    if "Name" not in values:
        for key, value in values.items():
            if key.startswith("Name[") and value:
                values["Name"] = value
                break
    return values


def xdg_desktop_dir(home: Path = HOME, config_path: Path | None = None) -> Path:
    config_path = config_path or home / ".config" / "user-dirs.dirs"
    try:
        lines = config_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return home / "Desktop"

    for raw_line in lines:
        if not raw_line.startswith("XDG_DESKTOP_DIR="):
            continue
        value = raw_line.partition("=")[2].strip().strip('"')
        value = value.replace("$HOME", str(home)).replace("${HOME}", str(home))
        candidate = Path(os.path.expandvars(value)).expanduser()
        if candidate.is_absolute():
            return candidate
    return home / "Desktop"


def steam_app_id(exec_line: str) -> int | None:
    match = _STEAM_GAME_RE.search(exec_line or "")
    if not match:
        return None
    value = int(match.group(1))
    # Non-Steam shortcuts use a synthetic 64-bit ID; it is not a store app ID.
    return value if value <= 0xFFFFFFFF else None


def rpcs3_title_id(exec_line: str) -> str:
    match = _RPCS3_GAME_RE.search(exec_line or "")
    return match.group(1).upper() if match else ""


def _exec_program(exec_line: str) -> str:
    value = (exec_line or "").strip()
    if not value:
        return ""
    if value.startswith('"') and '"' in value[1:]:
        token = value[1 : value.find('"', 1)]
    else:
        token = value.split(maxsplit=1)[0]
    return Path(token).name.casefold()


def is_game_desktop_entry(values: dict[str, str]) -> bool:
    if values.get("Type", "Application").casefold() != "application":
        return False
    if values.get("Hidden", "false").casefold() == "true":
        return False
    if values.get("NoDisplay", "false").casefold() == "true":
        return False

    name = values.get("Name", "").strip().casefold()
    exec_line = values.get("Exec", "").strip()
    exec_folded = exec_line.casefold()
    if not name or not exec_line or name in _FRONTEND_NAMES:
        return False

    if any(pattern in exec_folded for pattern in _GAME_EXEC_PATTERNS):
        return True
    if rpcs3_title_id(exec_line):
        return True

    categories = {part.strip().casefold() for part in values.get("Categories", "").split(";") if part.strip()}
    program = _exec_program(exec_line)
    if program in _FRONTEND_EXECUTABLES:
        return False

    # Game-specific Wine commands and native launchers commonly only advertise
    # the standard Game category. The frontend exclusions above keep launchers
    # and catalogue managers out of the discovery list.
    return "game" in categories and (".exe" in exec_folded or "%" not in exec_line)


def _platform_details(values: dict[str, str]) -> tuple[str, str, list[str]]:
    exec_line = values.get("Exec", "")
    folded = exec_line.casefold()
    if rpcs3_title_id(exec_line):
        return "RPCS3", "RPCS3 — PlayStation 3", ["Local", "Emulated", "RPCS3", "PlayStation 3"]
    if "retroarch" in folded:
        return "RetroArch", "PC games", ["Local", "Emulated", "RetroArch"]
    if "dolphin-emu" in folded:
        return "Dolphin", "PC games", ["Local", "Emulated", "Dolphin"]
    if "steam://rungameid/" in folded:
        return "Steam", "PC games", ["Local", "PC", "Steam"]
    if "heroic://launch/" in folded:
        return "Heroic", "PC games", ["Local", "PC", "Heroic"]
    if "lutris:rungame" in folded:
        return "Lutris", "PC games", ["Local", "PC", "Lutris"]
    return "Desktop", "PC games", ["Local", "PC"]


def desktop_candidate(path: Path) -> dict[str, Any] | None:
    values = parse_desktop_entry(path)
    if not is_game_desktop_entry(values):
        return None

    title = values.get("Name", "").strip() or path.stem
    platform, collection, tags = _platform_details(values)
    comment = values.get("Comment", "").strip()
    if comment.casefold().startswith(("play this game on ", "play ")) or comment.casefold() == title.casefold():
        comment = ""
    record: dict[str, Any] = {
        "id": stable_id(path, title, [str(path)]),
        "title": title,
        "collection": collection,
        "files": [str(path)],
        "launch": "",
        "launch_mode": "desktop",
        "workdir": values.get("Path", ""),
        "genres": [],
        "tags": tags,
        "summary": comment,
        "description": "",
        "artwork_box_front": "",
        "artwork_background": "",
        "extras": {"x-pegasus-discovered-from": "desktop"},
        "origin": "desktop-discovery",
    }
    icon = values.get("Icon", "").strip()
    if icon and Path(icon).expanduser().is_file():
        record["artwork_box_front"] = str(Path(icon).expanduser())

    app_id = steam_app_id(values.get("Exec", ""))
    title_id = rpcs3_title_id(values.get("Exec", ""))
    if app_id is not None:
        record["extras"]["x-pegasus-steam-app-id"] = str(app_id)
    if title_id:
        record["extras"]["x-pegasus-rpcs3-title-id"] = title_id

    return {
        "path": str(path),
        "platform": platform,
        "steam_app_id": app_id,
        "rpcs3_title_id": title_id,
        "icon": icon,
        "record": normalise_record(record),
    }


def _canonical_path(value: str) -> str:
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return str(Path(value).expanduser())


def discover_desktop_games(
    entries: list[dict[str, Any]], desktop_dir: Path | None = None
) -> list[dict[str, Any]]:
    desktop_dir = desktop_dir or xdg_desktop_dir()
    existing_paths = {
        _canonical_path(str(file_path))
        for entry in entries
        for file_path in (entry.get("files") or [])
        if str(file_path).lower().endswith(".desktop")
    }
    existing_titles = {
        str(entry.get("title", "")).strip().casefold()
        for entry in entries
        if str(entry.get("title", "")).strip()
    }

    candidates: list[dict[str, Any]] = []
    try:
        paths = sorted(desktop_dir.glob("*.desktop"), key=lambda item: item.name.casefold())
    except OSError:
        return candidates

    for path in paths:
        if _canonical_path(str(path)) in existing_paths:
            continue
        candidate = desktop_candidate(path)
        if candidate is None:
            continue
        title_key = candidate["record"]["title"].strip().casefold()
        if title_key in existing_titles:
            continue
        candidates.append(candidate)
    return candidates
