from __future__ import annotations

import copy
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from .core import (
    APP_NAME,
    BACKUP_DIR,
    CATALOG_PATH,
    CLOUD_METADATA_PATH,
    DEFAULT_CATEGORIES,
    cloud_game_count,
    cloud_metadata_mtime,
    desktop_name,
    load_catalog,
    new_record,
    normalise_path,
    normalise_record,
    save_catalog,
    validate_record,
    write_pegasus_files,
)
from .artwork import enrich_game_record, search_steam_games
from .desktop import desktop_candidate, discover_desktop_games, xdg_desktop_dir


class GameManagerWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application):
        super().__init__(application=application, title=APP_NAME)
        self.set_default_size(1240, 820)
        self.set_size_request(900, 620)

        self.catalog = load_catalog()
        self.catalog["entries"] = [normalise_record(item) for item in self.catalog.get("entries", [])]
        self.current_id: str | None = None
        self._loading_form = False
        self._rebuilding_list = False
        self._visible_ids: list[str] = []
        self._pending_extras: dict[str, str] = {}
        self._dirty = False
        self._closing = False
        self._auto_scan_complete = False
        self._discovery_dialog: Gtk.Dialog | None = None
        self._discovery_state: dict[str, Any] | None = None
        self._manual_search_dialog: Gtk.Dialog | None = None
        self._manual_search_state: dict[str, Any] | None = None
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="pegasus-artwork")

        if not CATALOG_PATH.exists():
            save_catalog(self.catalog)

        self._build_ui()
        self._rebuild_game_list()
        self._show_empty_form()
        self._refresh_source_status()
        self.connect("close-request", self._on_close_request)
        GLib.idle_add(self._scan_desktop_on_startup)

    @property
    def entries(self) -> list[dict[str, Any]]:
        return self.catalog["entries"]

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)

        header = Gtk.HeaderBar()
        header.set_title_widget(Gtk.Label(label=APP_NAME))
        root.append(header)

        add_button = Gtk.Button(label="Add game")
        add_button.set_tooltip_text("Create a new Pegasus entry")
        add_button.connect("clicked", lambda _button: self._start_new_game())
        header.pack_start(add_button)

        scan_button = Gtk.Button(label="Scan Desktop")
        scan_button.set_tooltip_text("Find game shortcuts that are not yet in Pegasus")
        scan_button.connect("clicked", lambda _button: self._scan_desktop(manual=True))
        header.pack_start(scan_button)

        self.save_button = Gtk.Button(label="Save to Pegasus")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda _button: self._save_current())
        self.save_button.set_sensitive(False)
        header.pack_end(self.save_button)

        refresh_button = Gtk.Button(label="Reload list")
        refresh_button.set_tooltip_text("Reload the manager's saved catalogue")
        refresh_button.connect("clicked", lambda _button: self._reload_catalog())
        header.pack_end(refresh_button)

        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        split.set_wide_handle(True)
        root.append(split)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_margin_top(12)
        left.set_margin_bottom(12)
        left.set_margin_start(12)
        left.set_margin_end(8)
        split.set_start_child(left)
        split.set_resize_start_child(False)
        split.set_shrink_start_child(False)
        split.set_position(350)

        self.library_title = Gtk.Label(label="Managed games", xalign=0)
        self.library_title.add_css_class("heading")
        left.append(self.library_title)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search title or category")
        self.search_entry.connect("search-changed", lambda _entry: self._rebuild_game_list())
        left.append(self.search_entry)

        list_scroll = Gtk.ScrolledWindow()
        list_scroll.set_vexpand(True)
        list_scroll.set_hexpand(True)
        self.game_list = Gtk.ListBox()
        self.game_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.game_list.set_activate_on_single_click(True)
        self.game_list.connect("row-selected", self._on_row_selected)
        self.game_list.connect("row-activated", self._on_row_activated)
        list_scroll.set_child(self.game_list)
        left.append(list_scroll)

        self.source_status = Gtk.Label(xalign=0, wrap=True)
        self.source_status.add_css_class("dim-label")
        left.append(self.source_status)

        warning = Gtk.Label(
            label="Removing a title removes it from Pegasus only. The original game, shortcut, and artwork files are never deleted.",
            xalign=0,
            wrap=True,
        )
        warning.add_css_class("dim-label")
        left.append(warning)

        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_vexpand(True)
        right_scroll.set_hexpand(True)
        split.set_end_child(right_scroll)

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        form.set_margin_top(18)
        form.set_margin_bottom(24)
        form.set_margin_start(24)
        form.set_margin_end(30)
        right_scroll.set_child(form)

        title = Gtk.Label(label="Game details", xalign=0)
        title.add_css_class("title-2")
        form.append(title)

        self.title_entry = self._entry("Title", "The name shown in Pegasus")
        form.append(self._field_row("Title", self.title_entry))

        self.category_entry = self._entry("", "Type any collection name")
        self.category_entry.set_text(DEFAULT_CATEGORIES[0])
        form.append(self._field_row("Category / collection", self.category_entry))

        category_hint = Gtk.Label(
            label="Suggested: PC games, RetroArch — N64, Dolphin — GameCube, or RPCS3 — PlayStation 3. Xbox Cloud Gaming stays read-only because its sync service owns that collection.",
            xalign=0,
            wrap=True,
        )
        category_hint.add_css_class("dim-label")
        form.append(category_hint)

        self.file_entry = self._entry("", "Path to a desktop shortcut or game file")
        form.append(self._file_row("Desktop shortcut / game file", self.file_entry, "file"))

        self.desktop_check = Gtk.CheckButton(label="Launch the selected .desktop shortcut")
        self.desktop_check.connect("toggled", self._on_desktop_mode_toggled)
        form.append(self.desktop_check)

        self.launch_entry = self._entry("", "Example: steam steam://rungameid/123")
        form.append(self._field_row("Launch command override", self.launch_entry))

        launch_hint = Gtk.Label(
            label="Leave the override blank when using a desktop shortcut. The manager creates a safe launcher for it.",
            xalign=0,
            wrap=True,
        )
        launch_hint.add_css_class("dim-label")
        form.append(launch_hint)

        self.workdir_entry = self._entry("", "Optional working directory")
        form.append(self._field_row("Working directory", self.workdir_entry))

        self.summary_view = Gtk.TextView()
        self.summary_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.summary_view.set_top_margin(8)
        self.summary_view.set_bottom_margin(8)
        self.summary_view.set_left_margin(8)
        self.summary_view.set_right_margin(8)
        summary_scroll = Gtk.ScrolledWindow()
        summary_scroll.set_min_content_height(88)
        summary_scroll.set_max_content_height(150)
        summary_scroll.set_propagate_natural_height(True)
        summary_scroll.set_child(self.summary_view)
        form.append(self._field_row("Summary", summary_scroll))

        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        form.append(separator)

        artwork_heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        artwork_title = Gtk.Label(label="Artwork and filters", xalign=0)
        artwork_title.add_css_class("title-3")
        artwork_title.set_hexpand(True)
        artwork_heading.append(artwork_title)
        self.find_artwork_button = Gtk.Button(label="Find artwork")
        self.find_artwork_button.set_tooltip_text("Identify this title and download cover and background artwork")
        self.find_artwork_button.connect("clicked", lambda _button: self._find_artwork_for_form())
        artwork_heading.append(self.find_artwork_button)
        self.search_artwork_button = Gtk.Button(label="Search manually…")
        self.search_artwork_button.set_tooltip_text(
            "Search Steam and choose the exact game when automatic matching cannot identify it"
        )
        self.search_artwork_button.connect("clicked", lambda _button: self._show_manual_artwork_search())
        artwork_heading.append(self.search_artwork_button)
        form.append(artwork_heading)

        self.artwork_preview = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.cover_picture = Gtk.Picture()
        self.cover_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.cover_picture.set_can_shrink(True)
        self.cover_picture.set_size_request(150, 190)
        self.background_picture = Gtk.Picture()
        self.background_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.background_picture.set_can_shrink(True)
        self.background_picture.set_size_request(300, 190)
        self.artwork_preview.append(self.cover_picture)
        self.artwork_preview.append(self.background_picture)
        self.artwork_preview.set_visible(False)
        form.append(self.artwork_preview)

        self.box_entry = self._entry("", "Optional cover image")
        form.append(self._file_row("Box / cover artwork", self.box_entry, "image"))

        self.background_entry = self._entry("", "Optional background image")
        form.append(self._file_row("Background artwork", self.background_entry, "image"))

        self.genre_entry = self._entry("", "Comma-separated, for example Action, Adventure")
        form.append(self._field_row("Genres", self.genre_entry))

        self.tags_entry = self._entry("", "Comma-separated, for example Local, Emulator")
        form.append(self._field_row("Tags", self.tags_entry))

        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        form.append(action_box)
        self.remove_button = Gtk.Button(label="Remove from Pegasus")
        self.remove_button.add_css_class("destructive-action")
        self.remove_button.connect("clicked", lambda _button: self._confirm_remove())
        self.remove_button.set_sensitive(False)
        action_box.append(self.remove_button)

        backup_hint = Gtk.Label(
            label=f"Backups are kept in {BACKUP_DIR} before managed metadata is rewritten.",
            xalign=0,
            wrap=True,
        )
        backup_hint.add_css_class("dim-label")
        form.append(backup_hint)

        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.set_margin_top(6)
        self.status.set_margin_bottom(6)
        self.status.set_margin_start(12)
        self.status.set_margin_end(12)
        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        root.append(self.status)

        for entry in (
            self.title_entry,
            self.category_entry,
            self.file_entry,
            self.launch_entry,
            self.workdir_entry,
            self.box_entry,
            self.background_entry,
            self.genre_entry,
            self.tags_entry,
        ):
            entry.connect("changed", self._on_form_changed)
        self.summary_view.get_buffer().connect("changed", self._on_form_changed)
        self.desktop_check.connect("toggled", self._on_form_changed)

    @staticmethod
    def _entry(_label: str, placeholder: str) -> Gtk.Entry:
        entry = Gtk.Entry()
        entry.set_hexpand(True)
        entry.set_placeholder_text(placeholder)
        return entry

    @staticmethod
    def _field_row(label_text: str, widget: Gtk.Widget) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        label = Gtk.Label(label=label_text, xalign=0)
        label.add_css_class("heading")
        row.append(label)
        row.append(widget)
        return row

    def _on_form_changed(self, *_args: Any) -> None:
        if self._loading_form:
            return
        self._dirty = True
        self.save_button.set_sensitive(True)
        self._refresh_artwork_preview()

    def _set_clean(self) -> None:
        self._dirty = False
        self.save_button.set_sensitive(False)

    def _summary_text(self) -> str:
        buffer = self.summary_view.get_buffer()
        start, end = buffer.get_bounds()
        return buffer.get_text(start, end, False).strip()

    def _refresh_artwork_preview(self) -> None:
        visible = False
        for picture, entry in (
            (self.cover_picture, self.box_entry),
            (self.background_picture, self.background_entry),
        ):
            path = Path(entry.get_text()).expanduser()
            if path.is_file():
                picture.set_filename(str(path))
                picture.set_visible(True)
                visible = True
            else:
                picture.set_paintable(None)
                picture.set_visible(False)
        self.artwork_preview.set_visible(visible)

    def _file_row(self, label_text: str, entry: Gtk.Entry, kind: str) -> Gtk.Box:
        row = self._field_row(label_text, Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6))
        container = row.get_last_child()
        container.append(entry)
        button = Gtk.Button(label="Choose…")
        button.connect("clicked", lambda _button: self._choose_file(entry, kind))
        container.append(button)
        return row

    def _choose_file(self, target: Gtk.Entry, kind: str) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose desktop shortcut or game file" if kind == "file" else "Choose artwork")
        if kind == "image":
            image_filter = Gtk.FileFilter()
            image_filter.set_name("Images")
            for pattern in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.avif"):
                image_filter.add_pattern(pattern)
            dialog.set_default_filter(image_filter)
        dialog.open(self, None, self._on_file_chosen, (target, kind))

    def _on_file_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult, data: tuple[Gtk.Entry, str]) -> None:
        target, kind = data
        try:
            selected = dialog.open_finish(result)
        except GLib.Error:
            return
        path = selected.get_path()
        if not path:
            return
        target.set_text(path)
        if kind == "file" and path.lower().endswith(".desktop"):
            if not self.title_entry.get_text().strip() or self.title_entry.get_text().strip() == "New game":
                title = desktop_name(path)
                if title:
                    self.title_entry.set_text(title)
            self.desktop_check.set_active(True)
            self._set_status("Desktop shortcut selected; its launcher will be used when you save.")

    def _on_desktop_mode_toggled(self, button: Gtk.CheckButton) -> None:
        if self._loading_form:
            return
        enabled = button.get_active()
        self.launch_entry.set_sensitive(not enabled)
        if enabled:
            self.launch_entry.set_text("")

    def _refresh_source_status(self) -> None:
        count = cloud_game_count(CLOUD_METADATA_PATH)
        mtime = cloud_metadata_mtime(CLOUD_METADATA_PATH)
        self.library_title.set_text(f"Managed games  ·  {len(self.entries)}")
        self.source_status.set_text(
            f"Xbox Cloud Gaming: {count} auto-synced titles\n"
            f"Last cloud update: {mtime}\n"
            f"Desktop folder: {xdg_desktop_dir()}"
        )

    def _rebuild_game_list(self, select_current: bool = True) -> None:
        query = self.search_entry.get_text().strip().casefold() if hasattr(self, "search_entry") else ""
        self._rebuilding_list = True
        self._visible_ids.clear()
        child = self.game_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.game_list.remove(child)
            child = next_child

        target_row: Gtk.ListBoxRow | None = None
        for entry in sorted(self.entries, key=lambda item: item["title"].casefold()):
            search_blob = f"{entry['title']} {entry['collection']}".casefold()
            if query and query not in search_blob:
                continue
            row = Gtk.ListBoxRow()
            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row_box.set_margin_top(7)
            row_box.set_margin_bottom(7)
            row_box.set_margin_start(9)
            row_box.set_margin_end(9)
            row_box.append(Gtk.Label(label=entry["title"], xalign=0))
            category = Gtk.Label(label=entry["collection"], xalign=0)
            category.add_css_class("dim-label")
            row_box.append(category)
            row.set_child(row_box)
            row.set_selectable(True)
            row.set_activatable(True)
            self._visible_ids.append(entry["id"])
            self.game_list.append(row)
            if select_current and entry["id"] == self.current_id:
                target_row = row

        self._rebuilding_list = False
        if target_row is not None:
            self.game_list.select_row(target_row)

    def _game_id_for_row(self, row: Gtk.ListBoxRow) -> str | None:
        index = row.get_index()
        if 0 <= index < len(self._visible_ids):
            return self._visible_ids[index]
        return None

    def _load_selected_row(self, row: Gtk.ListBoxRow | None) -> None:
        if self._rebuilding_list or row is None:
            return
        game_id = self._game_id_for_row(row)
        if game_id:
            if self._dirty and game_id != self.current_id:
                self._rebuilding_list = True
                if self.current_id in self._visible_ids:
                    self.game_list.select_row(
                        self.game_list.get_row_at_index(self._visible_ids.index(self.current_id))
                    )
                else:
                    self.game_list.unselect_all()
                self._rebuilding_list = False
                self._show_error(
                    "Unsaved changes",
                    "Save the current game before switching to another title.",
                )
                return
            self.current_id = game_id
            entry = self._find_entry(game_id)
            if entry:
                self._load_form(entry)

    def _on_row_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        self._load_selected_row(row)

    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        # GTK emits row-activated for a single click when activate-on-single-click
        # is enabled. Loading here also handles clicks where row-selected is not
        # emitted because the row was already selected.
        self.game_list.select_row(row)
        self._load_selected_row(row)

    def _find_entry(self, game_id: str | None) -> dict[str, Any] | None:
        if not game_id:
            return None
        return next((item for item in self.entries if item["id"] == game_id), None)

    def _load_form(self, entry: dict[str, Any]) -> None:
        self._loading_form = True
        self.title_entry.set_text(entry["title"])
        self.category_entry.set_text(entry["collection"])
        self.file_entry.set_text(entry["files"][0] if entry["files"] else "")
        self.desktop_check.set_active(entry["launch_mode"] == "desktop")
        self.launch_entry.set_text(entry["launch"] if entry["launch_mode"] != "desktop" else "")
        self.launch_entry.set_sensitive(entry["launch_mode"] != "desktop")
        self.workdir_entry.set_text(entry["workdir"])
        self.summary_view.get_buffer().set_text(entry["summary"])
        self.box_entry.set_text(entry["artwork_box_front"])
        self.background_entry.set_text(entry["artwork_background"])
        self.genre_entry.set_text(", ".join(entry["genres"]))
        self.tags_entry.set_text(", ".join(entry["tags"]))
        self._pending_extras = dict(entry["extras"])
        self.remove_button.set_sensitive(True)
        self._loading_form = False
        self._refresh_artwork_preview()
        self._set_clean()
        self._set_status(f"Editing {entry['title']}. Save to write the Pegasus metadata.")

    def _show_empty_form(self) -> None:
        self._loading_form = True
        self.title_entry.set_text("")
        self.category_entry.set_text(DEFAULT_CATEGORIES[0])
        self.file_entry.set_text("")
        self.desktop_check.set_active(False)
        self.launch_entry.set_text("")
        self.launch_entry.set_sensitive(True)
        self.workdir_entry.set_text("")
        self.summary_view.get_buffer().set_text("")
        self.box_entry.set_text("")
        self.background_entry.set_text("")
        self.genre_entry.set_text("")
        self.tags_entry.set_text("")
        self._pending_extras = {}
        self.remove_button.set_sensitive(False)
        self._loading_form = False
        self._refresh_artwork_preview()
        self._set_clean()

    def _start_new_game(self) -> None:
        if self._dirty:
            self._show_error("Unsaved changes", "Save the current game before starting a new one.")
            return
        self.current_id = None
        self._show_empty_form()
        self.game_list.unselect_all()
        self.title_entry.grab_focus()
        self._set_status("New game: choose a shortcut or game file, artwork, and category, then save.")

    def _collect_form(self) -> dict[str, Any]:
        existing = self._find_entry(self.current_id)
        record = copy.deepcopy(existing) if existing else new_record()
        if not record["id"]:
            record["id"] = f"{uuid.uuid4().hex[:12]}"
        record["title"] = self.title_entry.get_text().strip()
        record["collection"] = self.category_entry.get_text().strip()
        selected_file = normalise_path(self.file_entry.get_text())
        record["files"] = [selected_file] if selected_file else []
        record["launch_mode"] = "desktop" if self.desktop_check.get_active() else "command"
        record["launch"] = "" if record["launch_mode"] == "desktop" else self.launch_entry.get_text().strip()
        record["workdir"] = normalise_path(self.workdir_entry.get_text())
        record["summary"] = self._summary_text()
        record["artwork_box_front"] = self.box_entry.get_text().strip()
        record["artwork_background"] = self.background_entry.get_text().strip()
        record["genres"] = [item.strip() for item in self.genre_entry.get_text().split(",") if item.strip()]
        record["tags"] = [item.strip() for item in self.tags_entry.get_text().split(",") if item.strip()]
        record["extras"] = dict(self._pending_extras)
        return normalise_record(record)

    def _save_current(self) -> None:
        if not self._dirty:
            self._set_status("There are no unsaved changes.")
            return
        record = self._collect_form()
        errors = validate_record(record)
        if errors:
            self._show_error("The game cannot be saved yet", "\n".join(f"• {error}" for error in errors))
            return

        new_entries = [item for item in self.entries if item["id"] != record["id"]]
        new_entries.append(record)
        candidate = dict(self.catalog)
        candidate["entries"] = new_entries
        try:
            written = write_pegasus_files(candidate)
            save_catalog(candidate)
        except (OSError, ValueError) as exc:
            self._show_error("Could not save the Pegasus catalogue", str(exc))
            return

        self.catalog = candidate
        self.current_id = record["id"]
        self._rebuild_game_list()
        self._refresh_source_status()
        self._set_clean()
        names = ", ".join(path.name for path in written)
        self._set_status(f"Saved {record['title']}. Updated: {names}. Backups are in {BACKUP_DIR}.")

    def _find_artwork_for_form(self) -> None:
        record = self._collect_form()
        if not record["title"]:
            self._show_error("A title is needed", "Enter a game title before looking for artwork.")
            return

        hints: dict[str, Any] = {}
        if record["files"] and record["files"][0].lower().endswith(".desktop"):
            candidate = desktop_candidate(Path(record["files"][0]))
            if candidate:
                hints = candidate
        fingerprint = (record["title"], record["files"][0] if record["files"] else "")
        self._set_artwork_actions_busy(True, "Finding…")
        self._set_status(f"Identifying {record['title']} and looking for artwork…")
        future = self._executor.submit(enrich_game_record, record, hints)
        future.add_done_callback(
            lambda completed: GLib.idle_add(self._finish_form_artwork, completed, fingerprint)
        )

    def _finish_form_artwork(self, future: Future, fingerprint: tuple[str, str]) -> bool:
        if self._closing:
            return False
        self._set_artwork_actions_busy(False)
        current_file = normalise_path(self.file_entry.get_text())
        if (self.title_entry.get_text().strip(), current_file) != fingerprint:
            self._set_status("Artwork lookup finished, but the form changed, so its result was not applied.")
            return False
        try:
            result = future.result()
        except Exception as exc:  # Keep a provider defect from taking down the GTK process.
            self._show_error("Artwork lookup failed", str(exc))
            return False

        record = result["record"]
        self._loading_form = True
        self.box_entry.set_text(record["artwork_box_front"])
        self.background_entry.set_text(record["artwork_background"])
        self.summary_view.get_buffer().set_text(record["summary"])
        self.genre_entry.set_text(", ".join(record["genres"]))
        self._pending_extras = dict(record["extras"])
        self._loading_form = False
        self._refresh_artwork_preview()
        self._on_form_changed()
        self._set_status(result["status"])
        return False

    def _set_artwork_actions_busy(self, busy: bool, busy_label: str = "Finding…") -> None:
        self.find_artwork_button.set_sensitive(not busy)
        self.find_artwork_button.set_label(busy_label if busy else "Find artwork")
        self.search_artwork_button.set_sensitive(not busy)

    def _show_manual_artwork_search(
        self,
        record: dict[str, Any] | None = None,
        on_apply_started: Callable[[dict[str, Any]], None] | None = None,
        on_applied: Callable[[dict[str, Any]], None] | None = None,
        transient_for: Gtk.Window | None = None,
    ) -> None:
        record = normalise_record(copy.deepcopy(record)) if record is not None else self._collect_form()
        if not record["title"]:
            self._show_error("A title is needed", "Enter a game title before searching for artwork.")
            return
        if self._manual_search_dialog is not None:
            self._manual_search_dialog.present()
            return

        dialog = Gtk.Dialog(
            title="Search for game artwork",
            transient_for=transient_for or self,
            modal=True,
        )
        dialog.set_default_size(720, 520)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        apply_button = dialog.add_button("Use selected artwork", Gtk.ResponseType.ACCEPT)
        apply_button.add_css_class("suggested-action")
        apply_button.set_sensitive(False)

        content = dialog.get_content_area()
        content.set_spacing(12)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        intro = Gtk.Label(
            label=(
                "Search the Steam Store and choose the exact game. The selected game's cover, "
                "background, summary, and genres will be downloaded where available."
            ),
            xalign=0,
            wrap=True,
        )
        content.append(intro)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search_entry = Gtk.SearchEntry()
        search_entry.set_hexpand(True)
        search_entry.set_text(record["title"])
        search_entry.set_placeholder_text("Search by game title")
        search_button = Gtk.Button(label="Search")
        search_row.append(search_entry)
        search_row.append(search_button)
        content.append(search_row)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        results_list = Gtk.ListBox()
        results_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        results_list.set_activate_on_single_click(False)
        scroller.set_child(results_list)
        content.append(scroller)

        progress_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        spinner = Gtk.Spinner()
        progress_row.append(spinner)
        search_status = Gtk.Label(xalign=0, wrap=True)
        search_status.set_hexpand(True)
        progress_row.append(search_status)
        content.append(progress_row)

        state: dict[str, Any] = {
            "closed": False,
            "request_id": 0,
            "results": [],
            "entry": search_entry,
            "search_button": search_button,
            "apply_button": apply_button,
            "results_list": results_list,
            "spinner": spinner,
            "status": search_status,
            "record": record,
            "on_apply_started": on_apply_started,
            "on_applied": on_applied,
        }
        search_button.connect("clicked", lambda _button: self._begin_manual_artwork_search(state))
        search_entry.connect("activate", lambda _entry: self._begin_manual_artwork_search(state))
        results_list.connect("row-selected", self._manual_result_selected, state)
        results_list.connect(
            "row-activated", lambda _list, _row: dialog.response(Gtk.ResponseType.ACCEPT)
        )
        dialog.connect("response", self._manual_search_response, state)
        self._manual_search_dialog = dialog
        self._manual_search_state = state
        dialog.present()
        self._begin_manual_artwork_search(state)

    @staticmethod
    def _clear_list_box(list_box: Gtk.ListBox) -> None:
        child = list_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            list_box.remove(child)
            child = next_child

    def _begin_manual_artwork_search(self, state: dict[str, Any]) -> None:
        if state["closed"]:
            return
        query = state["entry"].get_text().strip()
        if not query:
            state["status"].set_text("Enter a game title to search.")
            return

        state["request_id"] += 1
        request_id = state["request_id"]
        state["results"] = []
        self._clear_list_box(state["results_list"])
        state["entry"].set_sensitive(False)
        state["search_button"].set_sensitive(False)
        state["apply_button"].set_sensitive(False)
        state["spinner"].start()
        state["spinner"].set_visible(True)
        state["status"].set_text(f"Searching Steam for {query}…")
        future = self._executor.submit(search_steam_games, query)
        future.add_done_callback(
            lambda completed: GLib.idle_add(
                self._finish_manual_artwork_search, completed, state, request_id, query
            )
        )

    def _finish_manual_artwork_search(
        self,
        future: Future,
        state: dict[str, Any],
        request_id: int,
        query: str,
    ) -> bool:
        if self._closing or state["closed"] or request_id != state["request_id"]:
            return False
        state["entry"].set_sensitive(True)
        state["search_button"].set_sensitive(True)
        state["spinner"].stop()
        state["spinner"].set_visible(False)
        try:
            results = future.result()
        except Exception as exc:  # Keep provider/network failures inside the dialog.
            state["status"].set_text(f"Steam search is unavailable: {exc}")
            state["entry"].grab_focus()
            return False

        state["results"] = results
        for result in results:
            row = Gtk.ListBoxRow()
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            labels.set_margin_top(9)
            labels.set_margin_bottom(9)
            labels.set_margin_start(12)
            labels.set_margin_end(12)
            name = Gtk.Label(label=result["name"], xalign=0)
            name.add_css_class("heading")
            labels.append(name)
            platforms = ", ".join(result["platforms"]) or "platform not listed"
            detail = Gtk.Label(label=f"Steam app {result['app_id']}  ·  {platforms}", xalign=0)
            detail.add_css_class("dim-label")
            labels.append(detail)
            row.set_child(labels)
            row.set_selectable(True)
            row.set_activatable(True)
            state["results_list"].append(row)

        if results:
            state["status"].set_text(
                f"Found {len(results)} result{'s' if len(results) != 1 else ''} for {query}. Select the exact game."
            )
        else:
            state["status"].set_text("No Steam games found. Try a broader or different title.")
            state["entry"].grab_focus()
        return False

    @staticmethod
    def _manual_result_selected(
        _list: Gtk.ListBox,
        row: Gtk.ListBoxRow | None,
        state: dict[str, Any],
    ) -> None:
        state["apply_button"].set_sensitive(row is not None and not state["closed"])

    def _manual_search_response(
        self,
        dialog: Gtk.Dialog,
        response: int,
        state: dict[str, Any],
    ) -> None:
        selected: dict[str, Any] | None = None
        row = state["results_list"].get_selected_row()
        if response == Gtk.ResponseType.ACCEPT and row is not None:
            index = row.get_index()
            if 0 <= index < len(state["results"]):
                selected = state["results"][index]

        state["closed"] = True
        self._manual_search_dialog = None
        self._manual_search_state = None
        dialog.destroy()
        if selected is None:
            return

        record = normalise_record(copy.deepcopy(state["record"]))
        fingerprint = (record["title"], record["files"][0] if record["files"] else "")
        hints = {
            "steam_app_id": selected["app_id"],
            "manual_selection": True,
            "replace_artwork": True,
        }
        if state["on_apply_started"] is not None:
            state["on_apply_started"](selected)
        else:
            self._set_artwork_actions_busy(True, "Downloading…")
            self._set_status(f"Downloading artwork for the selected game, {selected['name']}…")
        future = self._executor.submit(enrich_game_record, record, hints)
        if state["on_applied"] is not None:
            future.add_done_callback(
                lambda completed: GLib.idle_add(
                    self._finish_manual_target_artwork,
                    completed,
                    record,
                    state["on_applied"],
                )
            )
        else:
            future.add_done_callback(
                lambda completed: GLib.idle_add(self._finish_form_artwork, completed, fingerprint)
            )

    def _finish_manual_target_artwork(
        self,
        future: Future,
        fallback_record: dict[str, Any],
        on_applied: Callable[[dict[str, Any]], None],
    ) -> bool:
        if self._closing:
            return False
        try:
            result = future.result()
        except Exception as exc:
            result = {
                "record": fallback_record,
                "status": "Artwork lookup failed; this game can still be added.",
                "error": str(exc),
            }
        on_applied(result)
        return False

    def _scan_desktop_on_startup(self) -> bool:
        if self._auto_scan_complete:
            return False
        self._auto_scan_complete = True
        candidates = discover_desktop_games(self.entries)
        if not candidates:
            return False

        names = ", ".join(candidate["record"]["title"] for candidate in candidates[:6])
        if len(candidates) > 6:
            names += f", and {len(candidates) - 6} more"
        dialog = Gtk.AlertDialog()
        dialog.set_message(
            f"Found {len(candidates)} game shortcut{'s' if len(candidates) != 1 else ''} not yet in Pegasus"
        )
        dialog.set_detail(f"{names}\n\nReview them to identify the games and download their artwork automatically.")
        dialog.set_buttons(["Later", "Review games"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(1)
        dialog.choose(self, None, self._auto_scan_response, candidates)
        return False

    def _auto_scan_response(
        self,
        dialog: Gtk.AlertDialog,
        result: Gio.AsyncResult,
        candidates: list[dict[str, Any]],
    ) -> None:
        try:
            response = dialog.choose_finish(result)
        except GLib.Error:
            return
        if response == 1:
            if self._dirty:
                self._show_error("Unsaved changes", "Save the current game before importing Desktop shortcuts.")
            else:
                self._show_discovery_dialog(candidates)

    def _scan_desktop(self, manual: bool = False) -> None:
        if self._dirty:
            self._show_error("Unsaved changes", "Save the current game before importing Desktop shortcuts.")
            return
        if self._discovery_dialog is not None:
            self._discovery_dialog.present()
            return
        candidates = discover_desktop_games(self.entries)
        if candidates:
            self._show_discovery_dialog(candidates)
            return
        if manual:
            dialog = Gtk.AlertDialog()
            dialog.set_message("Your Desktop is already in sync")
            dialog.set_detail("No new game shortcuts were found outside the managed Pegasus catalogue.")
            dialog.show(self)
        self._set_status("Desktop scan complete. No unadded game shortcuts were found.")

    def _show_discovery_dialog(self, candidates: list[dict[str, Any]]) -> None:
        dialog = Gtk.Dialog(title="Add games from Desktop", transient_for=self, modal=True)
        dialog.set_default_size(720, 480)
        dialog.add_button("Later", Gtk.ResponseType.CANCEL)
        add_button = dialog.add_button("Add selected games", Gtk.ResponseType.ACCEPT)
        add_button.add_css_class("suggested-action")
        add_button.set_sensitive(False)

        content = dialog.get_content_area()
        content.set_spacing(12)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        intro = Gtk.Label(
            label="Review the detected games. Selected games will be added together after identification and artwork lookup finish.",
            xalign=0,
            wrap=True,
        )
        content.append(intro)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        content.append(scroller)
        game_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroller.set_child(game_box)

        state: dict[str, Any] = {
            "closed": False,
            "remaining": len(candidates),
            "rows": [],
            "add_button": add_button,
        }
        for candidate in candidates:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.set_margin_top(6)
            row.set_margin_bottom(6)
            check = Gtk.CheckButton()
            check.set_active(True)
            check.set_valign(Gtk.Align.CENTER)
            row.append(check)

            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            labels.set_hexpand(True)
            title = Gtk.Label(label=candidate["record"]["title"], xalign=0)
            title.add_css_class("heading")
            labels.append(title)
            source = Gtk.Label(
                label=f"{candidate['platform']}  ·  {Path(candidate['path']).name}", xalign=0
            )
            source.add_css_class("dim-label")
            labels.append(source)
            lookup_status = Gtk.Label(label="Identifying game and finding artwork…", xalign=0, wrap=True)
            labels.append(lookup_status)
            manual_search = Gtk.Button(label="Search manually…")
            manual_search.set_halign(Gtk.Align.START)
            manual_search.set_visible(False)
            labels.append(manual_search)
            row.append(labels)

            spinner = Gtk.Spinner()
            spinner.start()
            spinner.set_valign(Gtk.Align.CENTER)
            row.append(spinner)
            game_box.append(row)
            game_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

            row_state = {
                "candidate": candidate,
                "check": check,
                "status": lookup_status,
                "spinner": spinner,
                "manual_search": manual_search,
                "result": None,
            }
            state["rows"].append(row_state)
            check.connect("toggled", lambda _check, current=state: self._update_discovery_add_button(current))
            manual_search.connect(
                "clicked",
                lambda _button, current=row_state, shared=state: self._search_discovery_artwork(
                    current, shared
                ),
            )
            future = self._executor.submit(enrich_game_record, candidate["record"], candidate)
            future.add_done_callback(
                lambda completed, current=row_state, shared=state: GLib.idle_add(
                    self._finish_discovery_artwork, completed, current, shared
                )
            )

        dialog.connect("response", self._discovery_response, state)
        self._discovery_dialog = dialog
        self._discovery_state = state
        dialog.present()

    def _finish_discovery_artwork(
        self,
        future: Future,
        row_state: dict[str, Any],
        state: dict[str, Any],
    ) -> bool:
        if state["closed"]:
            return False
        try:
            result = future.result()
        except Exception as exc:  # Provider failure should not prevent import.
            result = {
                "record": row_state["candidate"]["record"],
                "status": "Artwork lookup failed; this game can still be added.",
                "error": str(exc),
            }
        row_state["result"] = result
        row_state["spinner"].stop()
        row_state["spinner"].set_visible(False)
        row_state["status"].set_text(result["status"])
        row_state["manual_search"].set_visible(not bool(result.get("matched_title")))
        state["remaining"] -= 1
        self._update_discovery_add_button(state)
        return False

    def _search_discovery_artwork(
        self,
        row_state: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        if state["closed"] or row_state["result"] is None:
            return
        self._show_manual_artwork_search(
            record=row_state["result"]["record"],
            on_apply_started=lambda selected: self._manual_discovery_artwork_started(
                selected, row_state, state
            ),
            on_applied=lambda result: self._manual_discovery_artwork_finished(
                result, row_state, state
            ),
            transient_for=self._discovery_dialog,
        )

    def _manual_discovery_artwork_started(
        self,
        selected: dict[str, Any],
        row_state: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        if state["closed"]:
            return
        state["remaining"] += 1
        row_state["manual_search"].set_sensitive(False)
        row_state["spinner"].set_visible(True)
        row_state["spinner"].start()
        row_state["status"].set_text(f"Downloading artwork for {selected['name']}…")
        self._update_discovery_add_button(state)

    def _manual_discovery_artwork_finished(
        self,
        result: dict[str, Any],
        row_state: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        if state["closed"]:
            return
        row_state["result"] = result
        row_state["manual_search"].set_sensitive(True)
        row_state["manual_search"].set_visible(not bool(result.get("matched_title")))
        row_state["spinner"].stop()
        row_state["spinner"].set_visible(False)
        row_state["status"].set_text(result["status"])
        state["remaining"] = max(0, state["remaining"] - 1)
        self._update_discovery_add_button(state)

    @staticmethod
    def _update_discovery_add_button(state: dict[str, Any]) -> None:
        ready = state["remaining"] == 0
        selected = any(row["check"].get_active() for row in state["rows"])
        state["add_button"].set_sensitive(ready and selected)

    def _discovery_response(self, dialog: Gtk.Dialog, response: int, state: dict[str, Any]) -> None:
        state["closed"] = True
        self._discovery_dialog = None
        self._discovery_state = None
        selected = [
            row["result"]["record"]
            for row in state["rows"]
            if row["check"].get_active() and row["result"] is not None
        ]
        dialog.destroy()
        if response != Gtk.ResponseType.ACCEPT or not selected:
            self._set_status("Desktop import left unchanged. You can scan again at any time.")
            return

        existing_ids = {entry["id"] for entry in self.entries}
        selected = [normalise_record(record) for record in selected if record["id"] not in existing_ids]
        candidate_catalog = dict(self.catalog)
        candidate_catalog["entries"] = [*self.entries, *selected]
        try:
            written = write_pegasus_files(candidate_catalog)
            save_catalog(candidate_catalog)
        except (OSError, ValueError) as exc:
            self._show_error("Could not add the discovered games", str(exc))
            return

        self.catalog = candidate_catalog
        self.current_id = selected[0]["id"] if selected else None
        self._rebuild_game_list()
        if self.current_id:
            entry = self._find_entry(self.current_id)
            if entry:
                self._load_form(entry)
        self._refresh_source_status()
        names = ", ".join(record["title"] for record in selected)
        self._set_status(
            f"Added {names} to Pegasus with available artwork. Updated {len(written)} metadata file(s); reload Pegasus with F5."
        )

    def _confirm_remove(self) -> None:
        entry = self._find_entry(self.current_id)
        if not entry:
            return
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Remove {entry['title']} from Pegasus?")
        dialog.set_detail("This removes the manager's metadata entry only. It will not delete the original game, shortcut, or artwork.")
        dialog.set_buttons(["Cancel", "Remove from Pegasus"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        dialog.choose(self, None, self._remove_response, entry["id"])

    def _remove_response(self, dialog: Gtk.AlertDialog, result: Gio.AsyncResult, game_id: str) -> None:
        try:
            response = dialog.choose_finish(result)
        except GLib.Error:
            return
        if response != 1:
            return
        candidate = dict(self.catalog)
        candidate["entries"] = [item for item in self.entries if item["id"] != game_id]
        entry = self._find_entry(game_id)
        try:
            write_pegasus_files(candidate)
            save_catalog(candidate)
        except (OSError, ValueError) as exc:
            self._show_error("Could not remove the Pegasus entry", str(exc))
            return
        self.catalog = candidate
        self.current_id = None
        self._rebuild_game_list(select_current=False)
        self._show_empty_form()
        self._refresh_source_status()
        self._set_status(f"Removed {entry['title'] if entry else 'the game'} from Pegasus. Original files were left untouched.")

    def _reload_catalog(self) -> None:
        if self._dirty:
            self._show_error("Unsaved changes", "Save the current game before reloading the catalogue.")
            return
        self.catalog = load_catalog()
        self.catalog["entries"] = [normalise_record(item) for item in self.catalog.get("entries", [])]
        self.current_id = None
        self._rebuild_game_list(select_current=False)
        self._show_empty_form()
        self._refresh_source_status()
        self._set_status("Reloaded the manager catalogue. The auto-synced Xbox file remains untouched.")

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        self._closing = True
        if self._manual_search_state is not None:
            self._manual_search_state["closed"] = True
            self._manual_search_state = None
        if self._manual_search_dialog is not None:
            self._manual_search_dialog.destroy()
            self._manual_search_dialog = None
        if self._discovery_state is not None:
            self._discovery_state["closed"] = True
            self._discovery_state = None
        if self._discovery_dialog is not None:
            self._discovery_dialog.destroy()
            self._discovery_dialog = None
        self._executor.shutdown(wait=False, cancel_futures=True)
        return False

    def _set_status(self, message: str) -> None:
        self.status.set_text(message)

    def _show_error(self, message: str, detail: str) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_message(message)
        dialog.set_detail(detail)
        dialog.show(self)


class GameManagerApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="com.acm.PegasusGameManager", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        for name, callback, accelerators in (
            ("new", lambda *_args: self._with_window("_start_new_game"), ["<Primary>n"]),
            ("save", lambda *_args: self._with_window("_save_current"), ["<Primary>s"]),
            ("reload", lambda *_args: self._with_window("_reload_catalog"), ["<Primary>r"]),
            ("scan", lambda *_args: self._with_window("_scan_desktop", manual=True), ["<Primary>d"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            self.set_accels_for_action(f"app.{name}", accelerators)

    def _with_window(self, method_name: str, **kwargs: Any) -> None:
        window = self.props.active_window
        if isinstance(window, GameManagerWindow):
            getattr(window, method_name)(**kwargs)

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = GameManagerWindow(self)
        window.present()
