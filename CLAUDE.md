# Prompter

Teleprompter for Ubuntu (GNOME Shell 46–50, **Wayland**). The UI is in Ukrainian; see README.md for
user-facing behaviour and the architecture table.

- `prompter.py`: GTK4 + libadwaita app (stdlib + PyGObject only, no pip deps). Main window =
  script editor; `OverlayWindow` = undecorated translucent window with the current block.
  All actions are `Gio.SimpleAction`s on the app, so the window, `prompter ДІЯ` and the
  extension (D-Bus `org.gtk.Actions.Activate` on `org.andrii.Prompter`) share one code path.
- `extension/extension.js`: grabs shortcuts from `~/.config/prompter/shortcuts.json`
  (`grab_accelerator`, re-read on change), keeps the overlay `make_above` + `stick`, hands
  focus back when it appears, saves/restores its geometry in `overlay.json`. D-Bus
  `org.andrii.Prompter.Shell`: `ShortcutStatus`, `RefreshInput`.

## Gotchas (learned the hard way)
- The extension finds the overlay by `OVERLAY_TITLE` + GTK app id. Keep the title in
  `prompter.py` and `extension.js` in sync.
- Hiding the overlay unmaps it; every show is a new `Meta.Window` → re-adopted in `'shown'`.
- Click-through ("lock") = empty input region via `gdk_surface_set_input_region` through
  ctypes (PyGObject needs the extra `python3-gi-cairo` package for cairo regions).
- Mutter 50 doesn't re-pick the actor under the pointer when an input region changes
  (`meta_surface_actor_set_input_region` only stores it; Clutter caches the pick "clear
  area" until a relayout). So after lock/unlock the app calls `RefreshInput`, which
  `queue_relayout()`s the overlay actor. Tests must use **one persistent** virtual pointer;
  a fresh device per click bypasses that cache and hides the bug.
- A new extension only loads after re-login on Wayland; develop against the headless shell.
- The overlay is undecorated, so it has no resize edges of its own: a capture-phase
  `GestureClick` on the window calls `begin_resize` within `RESIZE_MARGIN` of an edge (or on
  the ◢ grip) and is DENIED elsewhere so `Gtk.WindowHandle` moves it. Window coords = surface
  coords (no shadow). Don't use `translate_coordinates`: PyGObject returns 2 values, not 3.
- Pages = non-empty blocks between `---` lines (`block_spans`); the editor's cursor drives
  the overlay page (`block_at`), hotkeys scroll the editor (`programmatic_cursor` guards it).
- Links `[title](url)` (`LINK`, `links_of`) become overlay buttons opened with
  `Gio.AppInfo.launch_default_for_uri` (no portal). When locked, the input region is the
  union of the link buttons' bounds (`set_input_region(surface, rects)`), recomputed two
  frames after the buttons change, then `RefreshInput`. The headless harness installs a
  fake https handler that appends to `$PROMPTER_TEST_URLS`.
- Virtual input in tests: the first events of a just-created virtual device are dropped,
  and pointer enter + button press in one batch make GTK miss the press. Create the device
  early and move before clicking (see `gui_smoke.py`), as a real mouse does.
- Editor views: "raw" (TextView) and "formatted" (read-only page cards, `build_page_cards`).
- Every name in `REMOTE_ACTIONS` is also a CLI word. Anything missing there is treated as
  a *file name* by `prompter ДІЯ` (that once opened a script called "preferences").

## Tests
`python3 -m pytest -o addopts=""` runs everything. `tests/integration/headless_shell.sh` starts an
isolated headless gnome-shell (own dbus, XDG dirs, keyfile GSettings) with this repo's extension
plus the test-only `unsafe-mode@prompter.test` (enables `Shell.Eval`; never install it for real).
Before a commit: run the full suite and extend `tests/integration/scenario.py` for new behaviour.
