"""In-process GUI checks of the shortcut editor, run inside headless_shell.sh
(a real display, an isolated config dir). Exits non-zero on the first failure."""

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import prompter as pr  # noqa: E402
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

failures = []


def check(cond, msg):
    print("ok -" if cond else "FAIL -", msg)
    if not cond:
        failures.append(msg)


def grabbed():
    return json.loads(pr.SHORTCUTS_FILE.read_text(encoding="utf-8"))


def press(dialog, keyval, mods=Gdk.ModifierType(0)):
    """Feeds a key press to the capture dialog's key controller."""
    controllers = dialog.observe_controllers()
    ctl = next(c for c in (controllers.get_item(i) for i in range(controllers.get_n_items()))
               if isinstance(c, Gtk.EventControllerKey))
    _ok, keys = Gdk.Display.get_default().map_keyval(keyval)
    ctl.emit("key-pressed", keyval, keys[0].keycode, mods)


def record(app, action, keyval, mods=Gdk.ModifierType(0), response="save"):
    capture = app.capture_shortcut(action)
    press(capture, keyval, mods)
    can_save = capture.get_response_enabled("save")
    capture.emit("response", response)
    capture.force_close()
    return can_save


def run_checks(app):
    try:
        app.on_shortcuts()
        check(app.prefs["dialog"].get_visible_page().get_title() == "Клавіші",
              "⌨ opens the preferences on the Клавіші page")

        capture = app.capture_shortcut("next")
        check(grabbed() == {}, "global shortcuts are released while a combination is recorded")
        capture.emit("response", "cancel")
        capture.force_close()

        check(record(app, "next", Gdk.KEY_Page_Down), "PgDn without modifiers can be saved")
        check(app.shortcuts["next"] == "Page_Down", "the recorded key is used")
        check(grabbed()["next"] == "Page_Down", "the extension gets the new key")
        check(json.loads(pr.SETTINGS_FILE.read_text())["shortcuts"]["next"] == "Page_Down",
              "shortcuts are kept in settings.json")
        check(app.prefs["labels"]["next"].get_label() == "PgDn", "the row shows the new key")

        record(app, "prev", Gdk.KEY_n, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SUPER_MASK)
        check(app.shortcuts["prev"] == "<Control><Super>n", "a modifier combination is recorded (⌃⌘N)")

        check(not record(app, "toggle", Gdk.KEY_Page_Down, response="cancel"),
              "a key used by another action is rejected")
        check(not record(app, "toggle", Gdk.KEY_a, response="cancel"),
              "a plain letter without modifiers is rejected")
        check(grabbed()["toggle"] == pr.DEFAULT_SHORTCUTS["toggle"] and grabbed()["next"] == "Page_Down",
              "cancel restores the grabbed shortcuts unchanged")

        app.prefs["dialog"].force_close()
        app.win.close()
        check(not app.win.get_visible() and app.overlay.get_visible(),
              "closing the editor hides it; the app and overlay keep running")
    except Exception as e:  # report and fail the run
        traceback.print_exc()
        failures.append(repr(e))
    app.on_quit()
    return GLib.SOURCE_REMOVE


app = pr.Prompter()
app.connect("activate", lambda a: GLib.timeout_add(1500, run_checks, a))
app.run([sys.argv[0]])
if failures:
    sys.exit(1)
print("ALL GUI CHECKS PASSED")
