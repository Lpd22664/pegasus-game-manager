# Pegasus Game Manager

A local GTK4 application for maintaining the manually curated part of a Pegasus library.

It discovers game shortcuts on the Linux Desktop, identifies new titles, downloads matching cover/background artwork, and lets you review them before adding them to Pegasus. It also supports manual entries and editing. Removing an entry changes Pegasus metadata only; it never deletes the original game, shortcut, or artwork.

## Automatic Desktop import

- The app scans the XDG Desktop directory whenever it starts and prompts when it finds game-specific `.desktop` shortcuts that are not in the catalogue.
- Steam, Pegasus, and standalone emulator/frontend launchers are excluded. Shortcuts that launch a particular Steam, RPCS3, Heroic, Lutris, Wine, or native game are eligible.
- The review window selects all candidates by default and identifies/downloads artwork in background workers so the UI stays responsive.
- Exact platform IDs are used when present. RPCS3's installed `ICON0.PNG` and `PIC1.PNG` take priority; other games use a confidence-scored Steam Store match.
- Ambiguous title matches are not accepted automatically. The game remains importable and can be corrected with the normal editor.
- When automatic matching misses or rejects a game, `Search manually…` opens the Steam results so you can search for a different title and select the exact game whose artwork should be used.
- `Scan Desktop` (`Ctrl+D`) reruns discovery on demand.

Downloaded files live under `~/.local/share/pegasus-game-manager/artwork/`. The lookup needs an internet connection but no API key. A network/provider failure never prevents importing or launching a game.

## Ownership model

- `PC games` and `RPCS3 — PlayStation 3` are imported from the existing Pegasus metadata on first run and then managed by the app.
- Other categories are written to `managed.metadata.pegasus.txt`.
- The weekly `Xbox Cloud Gaming` catalogue remains owned by its sync service and is shown as read-only in the app. This prevents a manual save from overwriting the generated catalogue.
- Every metadata rewrite creates a timestamped copy under `~/.local/share/pegasus-game-manager/backups/`.

## Run

```text
./pegasus-game-manager
```

The desktop entry is installed as `Pegasus Game Manager`. Its launcher focuses the existing window if it is on another Hyprland workspace, or starts a new instance if needed. On Hyprland 0.55+, it uses the current Lua dispatch syntax and raises the manager above floating terminals. The Hyprland window rule also starts new manager windows floating and centered.

## Add a game

For a detected Desktop game, open the app, choose `Review games` in the startup prompt, confirm the selected titles, and press `Add selected games`. Artwork, summaries, genres, tags, and safe shortcut launchers are filled where available.

For a manual game:

1. Press `Add game` (`Ctrl+N`).
2. Choose a `.desktop` shortcut or a game/ROM file.
3. Select or type a category.
4. Press `Find artwork` to identify the title and fill its artwork/metadata. If it cannot find a confident match, press `Search manually…`, search for the game, select the exact Steam result, and choose `Use selected artwork`. You can also choose local images directly.
5. Leave `Launch the selected .desktop shortcut` enabled for a desktop shortcut, or enter a launch command instead.
6. Press `Save to Pegasus` (`Ctrl+S`), then reload Pegasus with `F5`.

Click any existing title in the left-hand list to load its details for editing or removal.

The manager creates a small launcher script for desktop shortcuts so paths with spaces and desktop-file placeholders are handled safely.

The manual result you select is remembered by its Steam app ID, so later artwork refreshes use that exact game instead of guessing again. The editor previews local artwork, exposes the game summary, preserves additional Pegasus asset fields, tracks unsaved changes, and prevents manual entries from being written into the sync-owned Xbox Cloud collection. `Ctrl+R` reloads the manager catalogue.

## Test

```text
python -m unittest discover -s tests
```

The tests cover Pegasus parsing/rendering, desktop-file parsing and filtering, catalogue de-duplication, platform IDs, automatic title-match confidence, manual search candidates and selection, artwork enrichment/failure behavior, validation, and launcher generation.
