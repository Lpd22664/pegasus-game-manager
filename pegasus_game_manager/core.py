from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


APP_NAME = "Pegasus Game Manager"
HOME = Path.home()
PEGASUS_GAMES_DIR = (
    HOME
    / ".var"
    / "app"
    / "org.pegasus_frontend.Pegasus"
    / "config"
    / "pegasus-frontend"
    / "games"
)
DATA_DIR = HOME / ".local" / "share" / "pegasus-game-manager"
CATALOG_PATH = DATA_DIR / "catalog.json"
BACKUP_DIR = DATA_DIR / "backups"
LAUNCHER_DIR = DATA_DIR / "launchers"
ARTWORK_DIR = DATA_DIR / "artwork"
CATALOG_VERSION = 2

CLOUD_METADATA_PATH = PEGASUS_GAMES_DIR / "xbox-cloud.metadata.pegasus.txt"
MANAGED_METADATA_PATH = PEGASUS_GAMES_DIR / "managed.metadata.pegasus.txt"
MANAGED_COLLECTION_FILES = {
    "PC games": PEGASUS_GAMES_DIR / "metadata.pegasus.txt",
    "RPCS3 — PlayStation 3": PEGASUS_GAMES_DIR / "rpcs3.metadata.pegasus.txt",
}
INITIAL_IMPORT_FILES = tuple(MANAGED_COLLECTION_FILES.values())

DEFAULT_CATEGORIES = [
    "PC games",
    "RetroArch — NES",
    "RetroArch — SNES",
    "RetroArch — N64",
    "Dolphin — GameCube",
    "Dolphin — Wii",
    "RPCS3 — PlayStation 3",
]

RESERVED_COLLECTIONS = {"Xbox Cloud Gaming"}

_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]*):(?:[ \t](.*))?$")
_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def is_uri(value: str) -> bool:
    return bool(_URI_RE.match(value.strip()))


def normalise_path(value: str) -> str:
    value = (value or "").strip()
    if not value or is_uri(value):
        return value
    return str(Path(value).expanduser())


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "game"


def stable_id(metadata_path: Path, title: str, files: list[str]) -> str:
    seed = f"{metadata_path}:{title}:{'|'.join(files)}".encode("utf-8")
    digest = hashlib.sha1(seed).hexdigest()[:10]
    return f"{slugify(title)[:42]}-{digest}"


def new_record(title: str = "") -> dict[str, Any]:
    return {
        "id": "",
        "title": title,
        "collection": "PC games",
        "files": [],
        "launch": "",
        "launch_mode": "command",
        "workdir": "",
        "genres": [],
        "tags": [],
        "summary": "",
        "description": "",
        "artwork_box_front": "",
        "artwork_background": "",
        "extras": {},
        "origin": "",
    }


def normalise_record(record: dict[str, Any]) -> dict[str, Any]:
    result = new_record()
    result.update(record)
    result["title"] = str(result.get("title", "")).strip()
    result["collection"] = str(result.get("collection", "PC games")).strip()
    result["files"] = [
        normalise_path(str(item))
        for item in (result.get("files") or [])
        if str(item).strip()
    ]
    result["launch"] = str(result.get("launch", "")).strip()
    result["launch_mode"] = str(result.get("launch_mode", "command"))
    if result["launch_mode"] not in {"command", "desktop"}:
        result["launch_mode"] = "command"
    result["workdir"] = normalise_path(str(result.get("workdir", "")))
    result["genres"] = [str(item).strip() for item in (result.get("genres") or []) if str(item).strip()]
    result["tags"] = [str(item).strip() for item in (result.get("tags") or []) if str(item).strip()]
    result["summary"] = str(result.get("summary", "")).strip()
    result["description"] = str(result.get("description", "")).strip()
    result["artwork_box_front"] = str(result.get("artwork_box_front", "")).strip()
    result["artwork_background"] = str(result.get("artwork_background", "")).strip()
    result["extras"] = {
        str(key): str(value)
        for key, value in (result.get("extras") or {}).items()
        if (str(key).startswith("x-") or str(key).startswith("assets.")) and str(value).strip()
    }
    result["origin"] = str(result.get("origin", ""))
    if not result.get("id"):
        result["id"] = stable_id(Path(result["origin"] or "managed"), result["title"], result["files"])
    return result


def _append_text(record: dict[str, Any], key: str, value: str) -> None:
    if record.get(key):
        record[key] += "\n" + value
    else:
        record[key] = value


def parse_metadata_file(path: Path) -> dict[str, Any]:
    """Parse the Pegasus fields needed by the manager.

    Unknown fields are retained as x- fields when possible, while unsupported
    metadata is ignored rather than making the whole file unusable.
    """

    collections: dict[str, dict[str, str]] = {}
    records: list[dict[str, Any]] = []
    current_collection: str | None = None
    current_record: dict[str, Any] | None = None
    pending_key: str | None = None

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {"collections": {}, "entries": []}

    for raw_line in lines:
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        if raw_line[0].isspace() and pending_key:
            value = raw_line.strip()
            if current_record is not None and pending_key in {"summary", "description"}:
                _append_text(current_record, pending_key, value)
            continue

        match = _KEY_RE.match(raw_line)
        if not match:
            pending_key = None
            continue

        key = match.group(1).lower()
        value = (match.group(2) or "").strip()
        pending_key = key

        if key == "collection":
            current_collection = value
            current_record = None
            collections.setdefault(value, {})
            continue

        if key == "game":
            current_record = new_record(value)
            current_record["collection"] = current_collection or "PC games"
            records.append(current_record)
            continue

        if current_record is not None:
            if key in {"file", "files"}:
                current_record["files"].append(value)
            elif key in {"genre", "genres"}:
                current_record["genres"].append(value)
            elif key in {"tag", "tags"}:
                current_record["tags"].append(value)
            elif key in {"launch", "command"}:
                current_record["launch"] = value
            elif key in {"workdir", "cwd"}:
                current_record["workdir"] = value
            elif key == "summary":
                current_record["summary"] = value
            elif key == "description":
                current_record["description"] = value
            elif key == "assets.box_front":
                current_record["artwork_box_front"] = value
            elif key == "assets.background":
                current_record["artwork_background"] = value
            elif key.startswith("assets."):
                current_record.setdefault("extras", {})[key] = value
            elif key.startswith("x-"):
                current_record.setdefault("extras", {})[key] = value
            continue

        if current_collection is not None:
            if key in {"shortname", "launch", "command", "workdir", "cwd", "summary", "description"}:
                collections[current_collection][key] = value

    for record in records:
        record["origin"] = str(path)
        if not record["launch"] and record["files"]:
            record["launch_mode"] = "desktop" if record["files"][0].lower().endswith(".desktop") else "command"
        record["id"] = stable_id(path, record["title"], record["files"])

    return {"collections": collections, "entries": [normalise_record(item) for item in records]}


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = [normalise_record(item) for item in raw.get("entries", [])]
            defaults = raw.get("collection_defaults", {})
            if not isinstance(defaults, dict):
                defaults = {}
            return {
                "version": CATALOG_VERSION,
                "entries": entries,
                "collection_defaults": defaults,
                "updated_at": raw.get("updated_at", ""),
            }
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            pass

    entries: list[dict[str, Any]] = []
    defaults: dict[str, dict[str, str]] = {}
    for metadata_path in INITIAL_IMPORT_FILES:
        parsed = parse_metadata_file(metadata_path)
        entries.extend(parsed["entries"])
        defaults.update(parsed["collections"])

    return {
        "version": CATALOG_VERSION,
        "entries": entries,
        "collection_defaults": defaults,
        "updated_at": "",
    }


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def save_catalog(catalog: dict[str, Any], path: Path = CATALOG_PATH) -> None:
    payload = {
        "version": CATALOG_VERSION,
        "entries": [normalise_record(item) for item in catalog.get("entries", [])],
        "collection_defaults": catalog.get("collection_defaults", {}),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def metadata_path_for(collection: str) -> Path:
    return MANAGED_COLLECTION_FILES.get(collection, MANAGED_METADATA_PATH)


def collection_shortname(collection: str) -> str:
    if collection == "PC games":
        return "pc-games"
    if collection == "RPCS3 — PlayStation 3":
        return "rpcs3"
    return slugify(collection)


def launcher_path(record: dict[str, Any]) -> Path:
    return LAUNCHER_DIR / f"{record['id']}.sh"


def write_desktop_launcher(record: dict[str, Any]) -> Path:
    files = record.get("files") or []
    if not files or not files[0].lower().endswith(".desktop"):
        raise ValueError("Desktop launch mode requires a .desktop shortcut as the first file.")
    shortcut = normalise_path(files[0])
    if not Path(shortcut).is_file():
        raise ValueError(f"Desktop shortcut does not exist: {shortcut}")

    path = launcher_path(record)
    content = "#!/usr/bin/env bash\nset -Eeuo pipefail\nexec /usr/bin/gio launch " + shlex.quote(shortcut) + "\n"
    atomic_write_text(path, content)
    path.chmod(0o755)
    return path


def desktop_name(path: str) -> str:
    if not path.lower().endswith(".desktop"):
        return ""
    try:
        for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
            if raw_line.startswith("Name="):
                return raw_line.partition("=")[2].strip()
    except (OSError, UnicodeError):
        pass
    return ""


def validate_record(record: dict[str, Any]) -> list[str]:
    record = normalise_record(record)
    errors: list[str] = []
    if not record["title"]:
        errors.append("Enter a game title.")
    if not record["collection"]:
        errors.append("Enter a category/collection.")
    elif record["collection"] in RESERVED_COLLECTIONS:
        errors.append(
            f"{record['collection']} is maintained by its automatic sync and cannot be edited here."
        )
    if not record["files"]:
        errors.append("Choose a desktop shortcut or game file.")
    else:
        first_file = record["files"][0]
        if not is_uri(first_file) and not Path(first_file).exists():
            errors.append(f"The selected game file does not exist: {first_file}")
    if record["launch_mode"] == "desktop":
        if not record["files"] or not record["files"][0].lower().endswith(".desktop"):
            errors.append("Desktop launch mode needs a .desktop shortcut as the first file.")
        elif not Path(record["files"][0]).is_file():
            errors.append("The selected desktop shortcut no longer exists.")
    elif not record["launch"]:
        errors.append("Enter a launch command, or choose a .desktop shortcut launch.")
    return errors


def _render_text_field(lines: list[str], key: str, value: str) -> None:
    value = value.strip()
    if not value:
        return
    parts = value.splitlines()
    lines.append(f"{key}: {parts[0]}")
    lines.extend(f"  {part}" for part in parts[1:])


def render_game(record: dict[str, Any]) -> list[str]:
    record = normalise_record(record)
    lines = [f"game: {record['title']}"]
    lines.extend(f"file: {item}" for item in record["files"])
    lines.extend(f"genre: {item}" for item in record["genres"])
    lines.extend(f"tag: {item}" for item in record["tags"])
    _render_text_field(lines, "summary", record["summary"])
    _render_text_field(lines, "description", record["description"])

    if record["launch_mode"] == "desktop":
        lines.append(f"launch: {launcher_path(record)}")
    elif record["launch"]:
        lines.append(f"launch: {record['launch']}")
    if record["workdir"]:
        lines.append(f"workdir: {record['workdir']}")
    if record["artwork_box_front"]:
        lines.append(f"assets.box_front: {record['artwork_box_front']}")
    if record["artwork_background"]:
        lines.append(f"assets.background: {record['artwork_background']}")
    lines.append(f"x-pegasus-game-manager-id: {record['id']}")
    for key, value in sorted(record["extras"].items()):
        if key not in {"x-pegasus-game-manager-id"} and value:
            lines.append(f"{key}: {value}")
    return lines


def render_metadata(entries: list[dict[str, Any]], defaults: dict[str, dict[str, str]] | None = None) -> str:
    defaults = defaults or {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        grouped[entry["collection"]].append(normalise_record(entry))

    lines = [
        "# This file is managed by Pegasus Game Manager.",
        "# Add/remove games in the manager so Pegasus launchers and artwork stay in sync.",
        "",
    ]
    for collection in sorted(grouped, key=str.casefold):
        lines.append(f"collection: {collection}")
        collection_default = defaults.get(collection, {})
        shortname = collection_default.get("shortname") or collection_shortname(collection)
        lines.append(f"shortname: {shortname}")
        collection_launch = collection_default.get("launch") or collection_default.get("command")
        collection_workdir = collection_default.get("workdir") or collection_default.get("cwd")
        if collection_launch:
            lines.append(f"launch: {collection_launch}")
        if collection_workdir:
            lines.append(f"workdir: {collection_workdir}")
        lines.append("")
        for entry in sorted(grouped[collection], key=lambda item: item["title"].casefold()):
            lines.extend(render_game(entry))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    destination = BACKUP_DIR / f"{timestamp}-{path.name}"
    if destination.exists():
        destination = BACKUP_DIR / f"{timestamp}-{time.time_ns()}-{path.name}"
    shutil.copy2(path, destination)
    return destination


def write_pegasus_files(catalog: dict[str, Any]) -> list[Path]:
    entries = [normalise_record(item) for item in catalog.get("entries", [])]
    for entry in entries:
        errors = validate_record(entry)
        if errors:
            raise ValueError("; ".join(errors))
        if entry["launch_mode"] == "desktop":
            write_desktop_launcher(entry)

    by_path: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        by_path[metadata_path_for(entry["collection"])].append(entry)

    written: list[Path] = []
    target_paths = set(by_path)
    for managed_path in (*MANAGED_COLLECTION_FILES.values(), MANAGED_METADATA_PATH):
        if managed_path.exists():
            target_paths.add(managed_path)

    for path in sorted(target_paths, key=str):
        backup_file(path)
        content = render_metadata(by_path.get(path, []), catalog.get("collection_defaults", {}))
        atomic_write_text(path, content)
        written.append(path)
    return written


def cloud_game_count(path: Path = CLOUD_METADATA_PATH) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.startswith("game: "))
    except OSError:
        return 0


def cloud_metadata_mtime(path: Path = CLOUD_METADATA_PATH) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
    except OSError:
        return "unknown"
