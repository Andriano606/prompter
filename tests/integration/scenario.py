"""End-to-end checks: the real app against the real extension. Must run inside
headless_shell.sh (one virtual 1920x1080 monitor). Exits non-zero on the first
failure."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from gi.repository import Gio, GLib

ROOT = Path(__file__).resolve().parents[2]
PROMPTER = ROOT / "prompter.py"
CONFIG = Path(GLib.get_user_config_dir()) / "prompter"
SCRIPT = CONFIG / "test.md"
OVERLAY_TITLE = "Prompter — суфлер"
MAIN_TITLE = "Prompter"
TOL = 4

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)


def wait_for(pred, timeout=10, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        res = pred()
        if res:
            return res
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("ok -", msg)


def ev(js):
    ok, out = bus.call_sync(
        "org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell", "Eval",
        GLib.Variant("(s)", (js,)), GLib.VariantType.new("(bs)"), 0, 5000, None).unpack()
    if not ok:
        raise AssertionError(f"Eval failed: {out}")
    return json.loads(out) if out else None


def find_window(title):
    return (f"global.get_window_actors().map(a => a.meta_window)"
            f".find(w => w.get_title() === {json.dumps(title)})")


def overlay():
    return ev(f"""(() => {{
        const w = {find_window(OVERLAY_TITLE)};
        if (!w) return null;
        const f = w.get_frame_rect();
        return {{above: w.is_above(), sticky: w.is_on_all_workspaces(),
                 x: f.x, y: f.y, width: f.width, height: f.height}};
    }})()""")


def focus_title():
    return ev("global.display.focus_window ? global.display.focus_window.get_title() : null")


def press(*keysyms):
    """Press keysyms as a chord through a virtual keyboard (real input path)."""
    ev(f"""
        const Clutter = imports.gi.Clutter;
        const seat = global.stage.context.get_backend().get_default_seat();
        const kb = seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        const keys = {json.dumps(list(keysyms))}.map(k => Clutter['KEY_' + k]);
        const t = () => imports.gi.GLib.get_monotonic_time();
        keys.forEach(k => kb.notify_keyval(t(), k, Clutter.KeyState.PRESSED));
        keys.slice().reverse().forEach(k => kb.notify_keyval(t(), k, Clutter.KeyState.RELEASED));
        true""")
    time.sleep(0.3)


def pointer(js):
    """Runs `js` with `p` = one virtual mouse that lives for the whole scenario
    (a fresh device per click would skip Clutter's pick cache, which is exactly
    what lock/unlock must get right), and `t` = current time."""
    ev(f"""
        const Clutter = imports.gi.Clutter;
        if (!global._prompterTestPointer)
            global._prompterTestPointer = global.stage.context.get_backend().get_default_seat()
                .create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        const p = global._prompterTestPointer;
        const t = imports.gi.GLib.get_monotonic_time();
        {js};
        true""")


def click(x, y):
    """Left click at stage coordinates, approaching like a real mouse."""
    for d in (40, 20, 0):
        pointer(f"p.notify_absolute_motion(t, {x - d}, {y - d})")
        time.sleep(0.1)
    pointer("p.notify_button(t, Clutter.BUTTON_PRIMARY, Clutter.ButtonState.PRESSED)")
    time.sleep(0.1)
    pointer("p.notify_button(t, Clutter.BUTTON_PRIMARY, Clutter.ButtonState.RELEASED)")
    time.sleep(0.4)


def drag(x, y, dx, dy):
    """Press at (x, y), move by (dx, dy) in steps like a real mouse, release."""
    for d in (20, 0):
        pointer(f"p.notify_absolute_motion(t, {x - d}, {y - d})")
        time.sleep(0.1)
    pointer("p.notify_button(t, Clutter.BUTTON_PRIMARY, Clutter.ButtonState.PRESSED)")
    time.sleep(0.2)
    for i in range(1, 9):
        pointer(f"p.notify_absolute_motion(t, {x + dx * i // 8}, {y + dy * i // 8})")
        time.sleep(0.08)
    pointer("p.notify_button(t, Clutter.BUTTON_PRIMARY, Clutter.ButtonState.RELEASED)")
    time.sleep(0.6)


def type_text(text):
    press_each = json.dumps(list(text))
    ev(f"""
        const Clutter = imports.gi.Clutter;
        const kb = global.stage.context.get_backend().get_default_seat()
            .create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        for (const ch of {press_each}) {{
            const k = Clutter['KEY_' + ch];
            kb.notify_keyval(imports.gi.GLib.get_monotonic_time(), k, Clutter.KeyState.PRESSED);
            kb.notify_keyval(imports.gi.GLib.get_monotonic_time(), k, Clutter.KeyState.RELEASED);
        }}
        true""")
    time.sleep(0.3)


def script_pages():
    return [b.strip() for b in SCRIPT.read_text(encoding="utf-8").split("---") if b.strip()]


def shortcut_status():
    try:
        res = bus.call_sync("org.andrii.Prompter.Shell", "/org/andrii/Prompter/Shell",
                            "org.andrii.Prompter.Shell", "ShortcutStatus", None,
                            GLib.VariantType.new("(s)"), 0, 1000, None)
    except GLib.Error:
        return None
    return json.loads(res.unpack()[0])


def position():
    try:
        settings = json.loads((CONFIG / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return settings["positions"].get(str(SCRIPT))


def cli(action):
    subprocess.run([sys.executable, str(PROMPTER), action], check=True)
    time.sleep(0.3)


def near(a, b):
    return abs(a - b) <= TOL


CONFIG.mkdir(parents=True, exist_ok=True)
LINK_PAGE = "Два\n[Демо](example.com/demo?a=1&b=2)"
SCRIPT.write_text(f"Один\n\n---\n\n{LINK_PAGE}\n\n---\n\nТри\n", encoding="utf-8")
app_log = open(CONFIG / "app.log", "w")
app = subprocess.Popen([sys.executable, str(PROMPTER), str(SCRIPT)], stderr=app_log)
try:
    o = wait_for(lambda: (o := overlay()) and o["above"] and o, what="overlay window on top")
    check(o["above"], "overlay is always on top")
    check(o["sticky"], "overlay is on all workspaces")
    wait_for(lambda: focus_title() == MAIN_TITLE, what="focus handed back to the main window")
    check(True, "showing the overlay does not steal focus")

    status = wait_for(lambda: (s := shortcut_status()) and len(s) == 9 and all(s.values()) and s,
                      what="all default shortcuts grabbed")
    check(True, f"extension grabbed the shortcuts from shortcuts.json ({len(status)})")

    press("Control_L", "Super_L", "Right")
    wait_for(lambda: position() == 1, what="next block via ⌃⌘→")
    press("Control_L", "Super_L", "Right")
    press("Control_L", "Super_L", "Left")
    wait_for(lambda: position() == 1, what="prev block via ⌃⌘←")
    check(True, "global next/prev shortcuts switch blocks")

    urls = Path(os.environ["PROMPTER_TEST_URLS"])
    press("Control_L", "Super_L", "o")
    wait_for(lambda: urls.exists() and urls.read_text().split() == ["https://example.com/demo?a=1&b=2"],
             what="⌃⌘O opens the page's link")
    check(True, "⌃⌘O opens the current page's link in the default browser")

    shortcuts = json.loads((CONFIG / "shortcuts.json").read_text(encoding="utf-8"))
    shortcuts["next"] = "<Control><Super>n"
    (CONFIG / "shortcuts.json").write_text(json.dumps(shortcuts), encoding="utf-8")
    time.sleep(0.8)
    press("Control_L", "Super_L", "Right")
    check(position() == 1, "old shortcut is released after shortcuts.json changes")
    press("Control_L", "Super_L", "n")
    wait_for(lambda: position() == 2, what="next block via the new shortcut")
    check(True, "new shortcut from shortcuts.json works")

    with SCRIPT.open("a", encoding="utf-8") as f:
        f.write("\n---\n\nЧотири\n")
    time.sleep(0.8)
    cli("next")
    wait_for(lambda: position() == 3, what="block added in an external editor")
    check(True, "script changes on disk are picked up")

    # "+ Нова сторінка" adds a page right after the one the cursor is in (still
    # the first one), the typed text lands there, and the overlay follows the cursor.
    ev(f"{find_window(MAIN_TITLE)}.activate(global.get_current_time()); true")
    wait_for(lambda: focus_title() == MAIN_TITLE, what="main window focused")
    cli("new-page")
    type_text("Xyz")
    wait_for(lambda: script_pages() == ["Один", "Xyz", LINK_PAGE, "Три", "Чотири"],
             what=f"new page saved after the first one (got {script_pages()})")
    check(True, "new page is inserted after the current page and saved")
    wait_for(lambda: position() == 1, what="overlay follows the page being edited")
    check(True, "overlay shows the page the cursor is in")

    press("Control_L", "Super_L", "h")
    wait_for(lambda: overlay() is None, what="overlay hidden via ⌃⌘H")
    press("Control_L", "Super_L", "h")
    o = wait_for(overlay, what="overlay shown via ⌃⌘H")
    check(o["above"], "toggle hides and shows the overlay; it stays on top")

    ev(f"{find_window(OVERLAY_TITLE)}.move_resize_frame(true, 300, 200, 800, 300); true")
    wait_for(lambda: (r := json.loads((CONFIG / "overlay.json").read_text()) if (CONFIG / "overlay.json").exists()
                      else None) and near(r["x"], 300) and near(r["y"], 200),
             what="overlay geometry saved")
    cli("hide")
    wait_for(lambda: overlay() is None, what="overlay hidden")
    cli("show")
    o = wait_for(lambda: (o := overlay()) and near(o["x"], 300) and near(o["y"], 200)
                 and near(o["width"], 800) and o, what="overlay restored at its saved place")
    check(True, f"overlay geometry is remembered ({o['x']},{o['y']} {o['width']}x{o['height']})")

    # Resizing an undecorated window: edges, corners and the ◢ grip; the middle moves.
    drag(300 + 800 - 4, 200 + 150, 120, 0)
    o = wait_for(lambda: (o := overlay()) and near(o["width"], 920) and o, what="right edge widens the overlay")
    check(near(o["x"], 300) and near(o["height"], 300), "dragging the right edge resizes (not moves)")
    drag(300 + 4, 200 + 300 - 4, -50, 60)
    o = wait_for(lambda: (o := overlay()) and near(o["x"], 250) and near(o["height"], 360) and o,
                 what="bottom-left corner resizes")
    check(near(o["width"], 970) and near(o["y"], 200), "dragging a corner resizes in both directions")
    drag(o["x"] + o["width"] - 32, o["y"] + o["height"] - 20, 40, 30)
    o = wait_for(lambda: (o := overlay()) and near(o["width"], 1010) and near(o["height"], 390) and o,
                 what="◢ grip resizes")
    check(True, "dragging the ◢ grip resizes")
    drag(o["x"] + o["width"] // 2, o["y"] + o["height"] // 2, 60, 40)
    o = wait_for(lambda: (o := overlay()) and near(o["x"], 310) and near(o["y"], 240) and o,
                 what="dragging the middle moves the overlay")
    check(near(o["width"], 1010), "dragging the middle moves without resizing")
    wait_for(lambda: json.loads((CONFIG / "overlay.json").read_text())["width"] == o["width"],
             what="resized geometry saved")
    check(True, "the new size is remembered")
    ev(f"{find_window(OVERLAY_TITLE)}.move_resize_frame(true, 300, 200, 800, 300); true")
    time.sleep(0.8)

    # Main window under the overlay: a click on the overlay goes to the overlay
    # when unlocked, and through it to the main window when locked.
    ev(f"{find_window(MAIN_TITLE)}.move_frame(true, 250, 150); true")
    time.sleep(0.5)
    cx, cy = 300 + 400, 200 + 150
    click(cx, cy)
    wait_for(lambda: focus_title() == OVERLAY_TITLE, what="click focuses the unlocked overlay")
    check(True, "unlocked overlay takes clicks")
    cli("lock")
    click(cx, cy)
    wait_for(lambda: focus_title() == MAIN_TITLE, what="click passes through the locked overlay")
    check(True, "locked overlay lets clicks through to the window below")
    ev(f"{find_window(OVERLAY_TITLE)}.move_resize_frame(true, 300, 200, 700, 260); true")
    time.sleep(0.8)
    ev(f"{find_window(OVERLAY_TITLE)}.activate(global.get_current_time()); true")
    wait_for(lambda: focus_title() == OVERLAY_TITLE, what="overlay focused programmatically")
    click(cx, cy)
    wait_for(lambda: focus_title() == MAIN_TITLE, what="click passes through after a resize")
    check(True, "click-through survives a resize")
    cli("lock")
    click(cx, cy)
    wait_for(lambda: focus_title() == OVERLAY_TITLE, what="unlocked overlay takes clicks again")
    check(True, "unlocking makes the overlay clickable again")

    # Closing the editor keeps the app running: shortcuts still work, and
    # starting Prompter again brings the editor back.
    before = position()
    ev(f"{find_window(MAIN_TITLE)}.delete(global.get_current_time()); true")
    wait_for(lambda: ev(f"!{find_window(MAIN_TITLE)}"), what="editor window closed")
    check(app.poll() is None and overlay() is not None, "closing the editor keeps the app and overlay running")
    press("Control_L", "Super_L", "Left")
    wait_for(lambda: position() == before - 1, what="shortcut works with the editor closed")
    check(True, "global shortcuts work with the editor closed")
    subprocess.run([sys.executable, str(PROMPTER)], check=True)
    wait_for(lambda: ev(f"!!{find_window(MAIN_TITLE)}"), what="editor reopened")
    check(True, "starting Prompter again reopens the editor")

    # A presentation clicker's key, without modifiers.
    shortcuts = json.loads((CONFIG / "shortcuts.json").read_text(encoding="utf-8"))
    shortcuts["next"] = "Page_Down"
    (CONFIG / "shortcuts.json").write_text(json.dumps(shortcuts), encoding="utf-8")
    time.sleep(0.8)
    check(shortcut_status().get("next") is True, "a key without modifiers (PgDn) can be grabbed")
    before = position()
    press("Page_Down")
    wait_for(lambda: position() == before + 1, what="PgDn turns the page")
    check(True, "PgDn turns the page globally")

    page = position()
    for dialog in ("preferences", "about"):
        cli(dialog)
        time.sleep(0.5)
        check(position() == page and json.loads((CONFIG / "settings.json").read_text())["file"] == str(SCRIPT),
              f"`prompter {dialog}` opens the dialog and keeps the script")
    cli("quit")
    app.wait(5)
    check(app.returncode == 0, "app quits cleanly")
    log = (CONFIG / "app.log").read_text(encoding="utf-8")
    check("Traceback" not in log, "no Python exceptions in the app (incl. preferences and about dialogs)")
finally:
    if app.poll() is None:
        app.kill()

print("ALL INTEGRATION CHECKS PASSED")
