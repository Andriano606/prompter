"""In-process GUI checks of the shortcut editor, run inside headless_shell.sh
(a real display, an isolated config dir). Exits non-zero on the first failure."""

import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import prompter as pr  # noqa: E402
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

failures = []
URLS = Path(os.environ["PROMPTER_TEST_URLS"])
LINK = "https://example.com/demo"
bus = None


def opened():
    return URLS.read_text().split() if URLS.exists() else []


def ev(js):
    ok, out = bus.call_sync("org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell", "Eval",
                            GLib.Variant("(s)", (js,)), GLib.VariantType.new("(bs)"), 0, 5000, None).unpack()
    if not ok:
        raise AssertionError(f"Eval failed: {out}")
    return json.loads(out) if out else None


def window(title):
    return f"global.get_window_actors().map(a => a.meta_window).find(w => w.get_title() === {json.dumps(title)})"


def pointer(js):
    """Runs `js` with `p` = one persistent virtual mouse and `t` = now."""
    ev(f"""
        const Clutter = imports.gi.Clutter;
        if (!global._prompterTestPointer)
            global._prompterTestPointer = global.stage.context.get_backend().get_default_seat()
                .create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        const p = global._prompterTestPointer, t = imports.gi.GLib.get_monotonic_time();
        {js};
        true""")


def click(x, y):
    """Moves the mouse onto (x, y), then clicks 300 ms later. Like a real mouse:
    pointer enter and button press in one batch make GTK miss the press."""
    pointer(f"p.notify_absolute_motion(t, {x - 15}, {y - 15}); p.notify_absolute_motion(t, {x}, {y})")
    GLib.timeout_add(300, lambda: pointer(
        "p.notify_button(t, Clutter.BUTTON_PRIMARY, Clutter.ButtonState.PRESSED);"
        "p.notify_button(t + 1000, Clutter.BUTTON_PRIMARY, Clutter.ButtonState.RELEASED)") and False)


def run_steps(steps):
    """Runs (delay_ms, fn) steps in order, letting the main loop work in between."""
    if not steps:
        return
    delay, fn = steps[0]

    def step():
        try:
            fn()
        except Exception as e:  # report and fail the run
            traceback.print_exc()
            failures.append(repr(e))
            steps[:] = steps[-1:]  # skip to the final step (quit)
        run_steps(steps[1:])
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(delay, step)


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
        check(json.loads(pr.SETTINGS_FILE.read_text())["shortcuts"] == pr.DEFAULT_SHORTCUTS,
              "settings.json holds the full set of shortcuts from the first start")
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
    link_steps(app)
    return GLib.SOURCE_REMOVE


def link_steps(app):
    """Link buttons on the overlay: clickable when unlocked, and still clickable
    when locked while the rest of the overlay lets clicks through."""
    global bus
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    state = {}

    def load():
        script = pr.CONFIG_DIR / "links.md"
        script.write_text(f"Сторінка з кнопкою\n[Демо]({LINK})\n", encoding="utf-8")
        app.load_file(str(script))
        app.win.present()
        ev(f"{window(pr.OVERLAY_TITLE)}.move_resize_frame(true, 300, 200, 800, 300); true")
        # The editor right under the overlay, to see where locked clicks go.
        ev(f"{window('Prompter')}.move_frame(true, 300, 200); true")
        # The first events of a just-created virtual device are dropped: create it now.
        pointer("p.notify_absolute_motion(t, 10, 10)")

    def place():
        check([b.get_label() for b in app.overlay.link_buttons] == ["↗  Демо"], "a link becomes a button on the overlay")
        frame = ev(f"(() => {{ const f = {window(pr.OVERLAY_TITLE)}.get_frame_rect(); return [f.x, f.y]; }})()")
        ok, r = app.overlay.link_buttons[0].compute_bounds(app.overlay)
        state["button"] = (frame[0] + int(r.get_x() + r.get_width() / 2), frame[1] + int(r.get_y() + r.get_height() / 2))
        state["text"] = (frame[0] + 400, frame[1] + 70)
        click(*state["button"])

    def unlocked_clicked():
        check(opened() == [LINK], "clicking the button opens the link in the browser")
        app.set_locked(True)

    def locked_click():
        click(*state["button"])

    def locked_clicked():
        check(opened() == [LINK, LINK], "the button stays clickable when the overlay is locked")
        click(*state["text"])

    def locked_text_clicked():
        focus = ev("global.display.focus_window ? global.display.focus_window.get_title() : null")
        check(focus == "Prompter", "elsewhere the locked overlay lets the click through")
        app.set_locked(False)

    def insert_link():
        dialog, title, url = app.on_insert_link()
        check(not dialog.get_response_enabled("insert"), "«Вставити» needs an address")
        title.set_text("Сайт")
        url.set_text("example.org/x")
        dialog.emit("response", "insert")
        dialog.force_close()
        check("[Сайт](https://example.org/x)" in app.text, "the dialog inserts [Напис](https://адреса)")

    def formatted_view():
        app.win.buffer.set_text("# Перша\n**жирно**\n\n---\n\nДруга [Сайт](example.org)\n\n---\n\nТретя\n")
        app.go_to(0)
        app.win.view_toggle.set_active_name("formatted")
        cards = app.win.page_cards
        check(app.win.stack.get_visible_child_name() == "formatted" and len(cards) == 3,
              "the formatted view shows one card per page")
        check(cards[0].has_css_class("current") and not cards[1].has_css_class("current"),
              "the current page's card is highlighted")
        texts = [c.get_first_child().get_next_sibling().get_label() for c in cards]
        check(texts[0] == "<b>Перша</b>\n<b>жирно</b>", "markdown is rendered (heading, bold)")
        controllers = cards[2].observe_controllers()
        click = next(c for c in (controllers.get_item(i) for i in range(controllers.get_n_items()))
                     if isinstance(c, Gtk.GestureClick))
        click.emit("released", 1, 5.0, 5.0)
        check(app.index == 2 and cards[2].has_css_class("current"), "clicking a card shows that page")
        check(json.loads(pr.SETTINGS_FILE.read_text())["editor_view"] == "formatted", "the chosen view is remembered")
        app.on_new_page()
        check(app.win.stack.get_visible_child_name() == "raw" and app.win.view_toggle.get_active_name() == "raw",
              "«Нова сторінка» switches back to the raw text")
        check(pr.split_blocks(app.text) == ["# Перша\n**жирно**", "Друга [Сайт](example.org)", "Третя"]
              and app.text.rstrip().endswith("---"), "…and adds the page after the current (last) one")

    def table_columns():
        check(not app.win.column.get_visible(), "no column picker without a table")
        app.win.buffer.set_text("# Заголовок\n\n| Українська | English |\n|---|---|\n"
                                "| **1. Вступ**<br>Привіт! | **1. Intro**<br>Hi! |\n"
                                "| **2. Демо**<br>[Відкрити X] | **2. Demo**<br>[Open X] |\n")
        model = app.win.column.get_model()
        check(app.win.column.get_visible()
              and [model.get_string(i) for i in range(model.get_n_items())] == ["Колонка: Українська", "Колонка: English"],
              "a table shows the column picker with its headers")
        check(app.blocks == ["# Заголовок", "**1. Вступ**\nПривіт!", "**2. Демо**\n[Відкрити X]"],
              "each table row is a page in the chosen column")
        app.win.column.set_selected(1)
        check(app.blocks[1:] == ["**1. Intro**\nHi!", "**2. Demo**\n[Open X]"]
              and json.loads(pr.SETTINGS_FILE.read_text())["table_column"] == 1,
              "picking English shows the English column and is remembered")
        app.go_to(1)
        check(app.overlay.text.get_label() == "<b>1. Intro</b>\nHi!", "the overlay shows the cell, <br> as a new line")

    def save():
        asked = []
        real_save_as = app.on_save_as
        app.on_save_as = lambda: asked.append(True)
        app.load_file(str(pr.DEFAULT_SCRIPT))
        check(app.is_untitled and "Не збережено" in app.win.title.get_subtitle(),
              "the first-start script counts as not saved anywhere")
        app.activate_action("save", None)
        check(asked == [True], "Ctrl+S on an unsaved script asks where to save")
        app.on_save_as = real_save_as
        dialog = app.save_dialog()
        check(dialog.get_modal() and dialog.get_initial_name() == "сценарій.md", "the save dialog is modal")

        script = pr.CONFIG_DIR / "saved.md"
        script.write_text("старе", encoding="utf-8")
        app.load_file(str(script))
        check(not app.is_untitled and app.win.title.get_subtitle() == "saved.md", "a chosen file is titled")
        app.win.buffer.set_text("нове")
        app.activate_action("save", None)  # right away, before the autosave delay
        check(script.read_text(encoding="utf-8") == "нове", "Ctrl+S writes the file immediately")

    run_steps([(300, save), (300, load), (1200, place), (1000, unlocked_clicked), (1000, locked_click),
               (1000, locked_clicked), (1000, locked_text_clicked), (300, insert_link),
               (300, formatted_view), (300, table_columns), (500, app.on_quit)])


app = pr.Prompter()
app.connect("activate", lambda a: GLib.timeout_add(1500, run_checks, a))
app.run([sys.argv[0]])
if failures:
    sys.exit(1)
print("ALL GUI CHECKS PASSED")
