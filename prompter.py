#!/usr/bin/env python3
"""Prompter — суфлер для GNOME (Wayland).

Головне вікно — редактор сценарію (сторінки розділяються рядком `---`).
Суфлер — окреме напівпрозоре вікно з поточною сторінкою. Тримати його поверх
усіх вікон і перехоплювати глобальні гарячі клавіші на Wayland може лише
GNOME Shell, тому це робить розширення з extension/ (див. README.md).
"""
import ctypes
import json
import re
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

VERSION = "2.0"
APP_ID = "org.andrii.Prompter"
APP_PATH = "/org/andrii/Prompter"
SHELL_BUS = "org.andrii.Prompter.Shell"
SHELL_PATH = "/org/andrii/Prompter/Shell"
# The extension recognises the overlay window by this title.
OVERLAY_TITLE = "Prompter — суфлер"

CONFIG_DIR = Path(GLib.get_user_config_dir()) / "prompter"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
SHORTCUTS_FILE = CONFIG_DIR / "shortcuts.json"  # read by the extension
DEFAULT_SCRIPT = CONFIG_DIR / "script.md"

# (дія, типова комбінація, підпис). На Mac-клавіатурі ⌃ = Control, ⌘ = Super.
ACTIONS = [
    ("toggle", "<Control><Super>h", "Показати / сховати суфлер"),
    ("next", "<Control><Super>Right", "Наступна сторінка"),
    ("prev", "<Control><Super>Left", "Попередня сторінка"),
    ("down", "<Control><Super>Down", "Прокрутити сторінку вниз"),
    ("up", "<Control><Super>Up", "Прокрутити сторінку вгору"),
    ("lock", "<Control><Super>l", "Закріпити / відкріпити (кліки крізь суфлер)"),
    ("bigger", "<Control><Super>equal", "Більший шрифт"),
    ("smaller", "<Control><Super>minus", "Менший шрифт"),
]
DEFAULT_SHORTCUTS = {name: accel for name, accel, _ in ACTIONS}
# All app actions — reachable from the window, `prompter ДІЯ` and D-Bus.
REMOTE_ACTIONS = [name for name, *_ in ACTIONS] + ["show", "hide", "new-page", "reload", "open",
                                                  "save-as", "preferences", "shortcuts", "about", "quit"]

# Клавіші, коли у фокусі саме вікно суфлера.
OVERLAY_KEYS = [
    ("Right|space|Page_Down", "app.next"),
    ("Left|BackSpace|Page_Up", "app.prev"),
    ("Down", "app.down"),
    ("Up", "app.up"),
    ("plus|equal|KP_Add", "app.bigger"),
    ("minus|KP_Subtract", "app.smaller"),
    ("Escape", "app.hide"),
]

DEFAULT_SETTINGS = {
    "file": None,
    "positions": {},
    "font_size": 44,
    "opacity": 0.6,
    "align": "left",
    "overlay_visible": True,
    "locked": False,
    "shortcuts": {},  # {action: accelerator}; empty = not migrated from shortcuts.json yet
}

SAMPLE_SCRIPT = """\
# Привіт! Це суфлер.
Пишіть сценарій тут — суфлер оновлюється одразу.

---

Нова сторінка — кнопка «+ Нова сторінка» внизу (або Ctrl+Enter).
У тексті сторінки розділяє рядок із трьох дефісів.
**Жирний текст** — як у Markdown.
// Рядок із // — примітка для себе (дрібна і сіра).

---

⌃⌘→ / ⌃⌘←  наступна / попередня сторінка
⌃⌘H  показати / сховати суфлер
⌃⌘L  закріпити: кліки проходять крізь суфлер
"""

SHORTCUT_MODIFIERS = ("<Control>", "<Primary>", "<Alt>", "<Super>")
# Keys that may be bound without a modifier: presentation clickers send PgUp/PgDn,
# and F-keys are rarely needed elsewhere during a call.
STANDALONE_KEYS = {"Page_Up", "Page_Down", "Pause", "Scroll_Lock", "Insert"} | {f"F{i}" for i in range(1, 25)}
KEY_SYMBOLS = {"Right": "→", "Left": "←", "Up": "↑", "Down": "↓", "equal": "=",
               "minus": "−", "plus": "+", "space": "Space", "Return": "↩",
               "Page_Up": "PgUp", "Page_Down": "PgDn"}


# --- pure helpers (unit-tested) ---------------------------------------------

def load_json(path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {**default, **data} if isinstance(data, dict) else dict(default)


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


SEPARATOR = re.compile(r"^[ \t]*-{3,}[ \t]*$", re.MULTILINE)
PAGE_BREAK = "\n\n---\n\n"


def block_spans(text):
    """(start, end) offsets of the non-empty pages (blocks) between `---` lines."""
    spans, start = [], 0
    for m in SEPARATOR.finditer(text):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(text)))
    return [(s, e) for s, e in spans if text[s:e].strip()]


def split_blocks(text):
    return [text[s:e].strip("\n") for s, e in block_spans(text)]


def block_at(text, offset):
    """Index of the page the cursor at `offset` belongs to; on a separator or
    in an empty page, the next page (or the last one)."""
    spans = block_spans(text)
    for i, (_start, end) in enumerate(spans):
        if offset <= end:
            return i
    return max(len(spans) - 1, 0)


def page_break_offset(text, offset):
    """Where "+ Нова сторінка" inserts PAGE_BREAK: right after the text of the
    page the cursor is in, so the new page comes next."""
    spans = block_spans(text)
    if not spans:
        return len(text.rstrip())
    start, end = spans[block_at(text, offset)]
    return start + len(text[start:end].rstrip())


def to_markup(block):
    lines = []
    for line in block.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("//"):
            note = GLib.markup_escape_text(stripped[2:].strip())
            lines.append(f'<span alpha="70%"><small><small>{note}</small></small></span>')
            continue
        if stripped.startswith("#"):
            line = "**" + stripped.lstrip("#").strip() + "**"
        escaped = GLib.markup_escape_text(line)
        lines.append(re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped))
    return "\n".join(lines)


def first_line(block):
    for line in block.splitlines():
        line = line.strip().lstrip("#").strip().replace("**", "")
        if line and not line.startswith("//"):
            return line
    return ""


def pretty_shortcut(accel):
    """Mac-style label: <Control><Super>Right → ⌃⌘→."""
    if not accel:
        return "—"
    ok, keyval, mods = Gtk.accelerator_parse(accel)
    if not ok:
        return accel
    text = ""
    for mask, symbol in ((Gdk.ModifierType.CONTROL_MASK, "⌃"), (Gdk.ModifierType.ALT_MASK, "⌥"),
                         (Gdk.ModifierType.SHIFT_MASK, "⇧"), (Gdk.ModifierType.SUPER_MASK, "⌘")):
        if mods & mask:
            text += symbol
    name = Gdk.keyval_name(keyval) or ""
    return text + KEY_SYMBOLS.get(name, name.upper() if len(name) == 1 else name)


def normalize_shortcut(accel):
    """Canonical form, so "<Super><Control>h" and "<Control><Super>h" compare equal."""
    ok, keyval, mods = Gtk.accelerator_parse(accel)
    return Gtk.accelerator_name(keyval, mods) if ok else accel


def is_standalone(accel):
    """A key from STANDALONE_KEYS bound without any modifier."""
    ok, keyval, mods = Gtk.accelerator_parse(accel)
    return ok and not mods and Gdk.keyval_name(keyval) in STANDALONE_KEYS


def validate_shortcut(accel, shortcuts, action):
    """Returns an error message, or None if the shortcut can be used."""
    if not any(m in accel for m in SHORTCUT_MODIFIERS) and not is_standalone(accel):
        return ("Потрібен модифікатор: ⌃ Control, ⌥ Option або ⌘ Cmd. "
                "Без модифікатора можна лише PageUp / PageDown і F1–F24")
    labels = {name: label for name, _, label in ACTIONS}
    for other, other_accel in shortcuts.items():
        if other != action and other_accel and \
                normalize_shortcut(other_accel) == normalize_shortcut(accel):
            return f"Ця комбінація вже призначена дії «{labels.get(other, other)}»"
    return None


# --- windows -----------------------------------------------------------------

def set_input_region(surface, empty):
    """gdk_surface_set_input_region(): an empty region lets clicks through,
    NULL restores the default. Called via ctypes because PyGObject needs the
    extra python3-gi-cairo package for cairo regions."""
    get_pointer = ctypes.pythonapi.PyCapsule_GetPointer
    get_pointer.restype = ctypes.c_void_p
    get_pointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
    gtk = ctypes.CDLL("libgtk-4.so.1")
    gtk.gdk_surface_set_input_region.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    region = None
    if empty:
        cairo = ctypes.CDLL("libcairo.so.2")
        cairo.cairo_region_create.restype = ctypes.c_void_p
        cairo.cairo_region_destroy.argtypes = [ctypes.c_void_p]
        region = cairo.cairo_region_create()
    gtk.gdk_surface_set_input_region(get_pointer(surface.__gpointer__, None), region)
    if region:
        cairo.cairo_region_destroy(region)  # GDK keeps its own copy


RESIZE_MARGIN = 16  # px from the overlay's edge where dragging resizes instead of moving
RESIZE_CURSORS = {"NORTH": "n-resize", "SOUTH": "s-resize", "WEST": "w-resize", "EAST": "e-resize",
                  "NORTH_WEST": "nw-resize", "NORTH_EAST": "ne-resize",
                  "SOUTH_WEST": "sw-resize", "SOUTH_EAST": "se-resize"}


def resize_edge(x, y, width, height, margin=RESIZE_MARGIN):
    """Gdk.SurfaceEdge name ("NORTH_WEST", "EAST", …) under (x, y), or None."""
    vertical = "NORTH" if y < margin else "SOUTH" if y >= height - margin else ""
    horizontal = "WEST" if x < margin else "EAST" if x >= width - margin else ""
    return "_".join(filter(None, (vertical, horizontal))) or None


class OverlayWindow(Gtk.Window):
    """Напівпрозоре вікно з поточною сторінкою. Перетягується за будь-яке місце,
    розмір змінюється за краї й кути (вікно без рамки, тож це робимо самі).
    У закріпленому стані порожній input region пропускає кліки до вікон під ним."""

    def __init__(self, app):
        super().__init__(application=app, title=OVERLAY_TITLE, decorated=False,
                         default_width=900, default_height=320)
        self.add_css_class("prompter-overlay")
        self.locked = False

        self.status = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.status.add_css_class("status")
        self.lock_icon = Gtk.Image(icon_name="changes-prevent-symbolic", visible=False)
        self.lock_icon.add_css_class("status")
        top = Gtk.Box(spacing=8)
        top.append(self.status)
        top.append(self.lock_icon)

        self.text = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                              xalign=0, yalign=0, valign=Gtk.Align.START)
        self.text.add_css_class("text")
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroller.set_child(self.text)

        self.preview = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.preview.add_css_class("preview")
        self.grip = Gtk.Label(label="◢", valign=Gtk.Align.END, tooltip_text="Тягніть, щоб змінити розмір")
        self.grip.add_css_class("grip")
        bottom = Gtk.Box(spacing=8)
        bottom.append(self.preview)
        bottom.append(self.grip)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card")
        card.append(top)
        card.append(self.scroller)
        card.append(bottom)
        self.set_child(Gtk.WindowHandle(child=card))

        keys = Gtk.ShortcutController()
        for trigger, action in OVERLAY_KEYS:
            keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string(trigger),
                                           action=Gtk.NamedAction.new(action)))
        self.add_controller(keys)

        # Capture phase: edges and the grip resize before Gtk.WindowHandle can
        # start a move. Window coordinates equal surface coordinates here
        # (no decorations, no shadow).
        press = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        press.connect("pressed", self.on_pressed)
        self.add_controller(press)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: self.set_cursor_from_name(
            RESIZE_CURSORS.get(self.edge_at(x, y))))
        motion.connect("leave", lambda _c: self.set_cursor_from_name(None))
        self.add_controller(motion)
        self.connect("realize", self.on_realize)

    def on_realize(self, _window):
        # GTK resets the input region when the window is resized.
        self.get_surface().connect("layout", lambda *_: self.apply_input_region())
        self.apply_input_region()

    def edge_at(self, x, y):
        if self.pick(x, y, Gtk.PickFlags.DEFAULT) is self.grip:
            return "SOUTH_EAST"
        return resize_edge(x, y, self.get_width(), self.get_height())

    def on_pressed(self, gesture, _n_press, x, y):
        edge = self.edge_at(x, y)
        if not edge:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.get_surface().begin_resize(getattr(Gdk.SurfaceEdge, edge), gesture.get_device(),
                                        gesture.get_current_button(), x, y,
                                        gesture.get_current_event_time())

    def set_locked(self, locked):
        self.locked = locked
        self.lock_icon.set_visible(locked)
        self.apply_input_region()

    def apply_input_region(self):
        surface = self.get_surface()
        if surface:
            set_input_region(surface, self.locked)

    def show_block(self, markup, status, preview, reset_scroll):
        self.text.set_markup(markup)
        self.status.set_text(status)
        self.preview.set_text(preview)
        if reset_scroll:
            self.scroller.get_vadjustment().set_value(0)

    def scroll(self, direction, step):
        adj = self.scroller.get_vadjustment()
        adj.set_value(adj.get_value() + direction * step)

    def set_align(self, align):
        center = align == "center"
        self.text.set_xalign(0.5 if center else 0)
        self.text.set_justify(Gtk.Justification.CENTER if center else Gtk.Justification.LEFT)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Prompter", default_width=760, default_height=620)

        self.title = Adw.WindowTitle(title="Prompter")
        header = Adw.HeaderBar(title_widget=self.title)
        header.pack_start(Gtk.Button(icon_name="document-open-symbolic", action_name="app.open",
                                     tooltip_text="Відкрити сценарій (Ctrl+O)"))
        menu = Gio.Menu()
        menu.append("Відкрити…", "app.open")
        menu.append("Зберегти як…", "app.save-as")
        section = Gio.Menu()
        section.append("Гарячі клавіші", "app.shortcuts")
        section.append("Налаштування", "app.preferences")
        section.append("Про програму", "app.about")
        section.append("Вийти", "app.quit")
        menu.append_section(None, section)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       primary=True, tooltip_text="Меню"))
        header.pack_end(Gtk.Button(icon_name="input-keyboard-symbolic", action_name="app.shortcuts",
                                   tooltip_text="Гарячі клавіші: призначити свої комбінації"))
        header.pack_end(Gtk.ToggleButton(icon_name="changes-prevent-symbolic", action_name="app.locked",
                                         tooltip_text="Закріпити суфлер: кліки проходять крізь нього"))
        overlay_btn = Gtk.ToggleButton(label="Суфлер", action_name="app.overlay",
                                       tooltip_text="Показати / сховати суфлер")
        overlay_btn.add_css_class("suggested-action")
        header.pack_end(overlay_btn)

        self.banner = Adw.Banner(
            title="Розширення Prompter для GNOME не активне: гарячі клавіші й «поверх усіх вікон» "
                  "запрацюють після повторного входу в систему")

        self.buffer = Gtk.TextBuffer()
        self.view = Gtk.TextView(buffer=self.buffer, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 top_margin=16, bottom_margin=16, left_margin=20, right_margin=20)
        self.view.add_css_class("editor")
        self.separator_tag = self.buffer.create_tag(
            "separator", foreground="#8f8f8f", weight=Pango.Weight.BOLD,
            justification=Gtk.Justification.CENTER, pixels_above_lines=6, pixels_below_lines=6)
        self.current_tag = self.buffer.create_tag("current", paragraph_background="rgba(53,132,228,0.10)")
        # Capture phase: otherwise the text view takes Ctrl+Enter as a newline.
        keys = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>Return|<Control>KP_Enter"),
                                       action=Gtk.NamedAction.new("app.new-page")))
        self.view.add_controller(keys)
        editor = Gtk.ScrolledWindow(vexpand=True, child=self.view)

        bar = Gtk.ActionBar()
        bar.pack_start(Gtk.Button(icon_name="go-previous-symbolic", action_name="app.prev",
                                  tooltip_text="Попередня сторінка"))
        self.position = Gtk.Label(width_chars=14)
        bar.pack_start(self.position)
        bar.pack_start(Gtk.Button(icon_name="go-next-symbolic", action_name="app.next",
                                  tooltip_text="Наступна сторінка"))
        new_page = Gtk.Button(child=Adw.ButtonContent(icon_name="list-add-symbolic", label="Нова сторінка"),
                              action_name="app.new-page",
                              tooltip_text="Додати сторінку після поточної (Ctrl+Enter)")
        new_page.add_css_class("flat")
        bar.pack_start(new_page)
        self.hint = Gtk.Button(action_name="app.shortcuts", tooltip_text="Змінити гарячі клавіші")
        self.hint.add_css_class("flat")
        self.hint.add_css_class("dim-label")
        bar.pack_end(self.hint)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.banner)
        box.append(editor)
        box.append(bar)
        toolbar = Adw.ToolbarView(content=box)
        toolbar.add_top_bar(header)
        self.toasts = Adw.ToastOverlay(child=toolbar)
        self.set_content(self.toasts)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))


# --- application -------------------------------------------------------------

class Prompter(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
        # settings.json keeps the shortcuts; shortcuts.json is only what the
        # extension grabs right now (emptied while a combination is recorded).
        saved = self.settings["shortcuts"] or load_json(SHORTCUTS_FILE, {})
        self.shortcuts = {**DEFAULT_SHORTCUTS, **{k: v for k, v in saved.items() if k in DEFAULT_SHORTCUTS}}
        self.prefs = None  # open preferences dialog, its shortcut rows and labels
        self.pending_test = None  # action whose new shortcut the user is about to try
        self.told_background = False
        self.win = None
        self.overlay = None
        self.path = None
        self.text = ""
        self.blocks = [""]
        self.index = 0
        self.file_monitor = None
        self.save_id = 0
        self.reload_id = 0
        self.programmatic_cursor = False
        self.css = Gtk.CssProvider()

    def do_startup(self):
        Adw.Application.do_startup(self)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.update_css()

        for name in REMOTE_ACTIONS:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, name=name: self.run_action(name))
            self.add_action(action)
        self.overlay_action = Gio.SimpleAction.new_stateful(
            "overlay", None, GLib.Variant.new_boolean(self.settings["overlay_visible"]))
        self.overlay_action.connect("change-state", lambda _a, v: self.set_overlay_visible(v.unpack()))
        self.add_action(self.overlay_action)
        self.locked_action = Gio.SimpleAction.new_stateful(
            "locked", None, GLib.Variant.new_boolean(self.settings["locked"]))
        self.locked_action.connect("change-state", lambda _a, v: self.set_locked(v.unpack()))
        self.add_action(self.locked_action)

        for action, accels in {"open": ["<Control>o"], "save-as": ["<Control><Shift>s"],
                               "preferences": ["<Control>comma"], "quit": ["<Control>q"],
                               "next": ["<Control>Page_Down"], "prev": ["<Control>Page_Up"],
                               "new-page": ["<Control>Return"]}.items():
            self.set_accels_for_action(f"app.{action}", accels)

        self.publish_shortcuts()
        Gio.bus_watch_name(Gio.BusType.SESSION, SHELL_BUS, Gio.BusNameWatcherFlags.NONE,
                           lambda *_: self.win and self.win.banner.set_revealed(False),
                           lambda *_: self.win and self.win.banner.set_revealed(True))

    def do_activate(self):
        self.ensure_windows()
        if self.path is None:
            self.load_file(self.settings["file"] or self.default_script())
        self.win.present()

    def do_open(self, files, _n_files, _hint):
        self.ensure_windows()
        self.load_file(files[0].get_path())
        self.win.present()

    def ensure_windows(self):
        if self.win:
            return
        self.win = MainWindow(self)
        self.win.banner.set_revealed(not self.shell_running())
        self.win.buffer.connect("changed", self.on_buffer_changed)
        self.win.buffer.connect("notify::cursor-position", self.on_cursor_moved)
        self.win.connect("close-request", self.on_main_close)
        self.overlay = OverlayWindow(self)
        self.overlay.set_align(self.settings["align"])
        self.overlay.set_locked(self.settings["locked"])
        self.update_hint()
        if self.settings["overlay_visible"]:
            # After the main window, so the extension hands focus back to it.
            GLib.idle_add(lambda: self.overlay.set_visible(self.settings["overlay_visible"]) and False)

    @staticmethod
    def default_script():
        if not DEFAULT_SCRIPT.exists():
            DEFAULT_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
            DEFAULT_SCRIPT.write_text(SAMPLE_SCRIPT, encoding="utf-8")
        return str(DEFAULT_SCRIPT)

    @staticmethod
    def shell_running():
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        try:
            owner = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                  "NameHasOwner", GLib.Variant("(s)", (SHELL_BUS,)),
                                  GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 1000, None)
        except GLib.Error:
            return False
        return owner.unpack()[0]

    @staticmethod
    def shortcut_status():
        """{action: bool} — whether the extension could grab each shortcut, or None."""
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        try:
            res = bus.call_sync(SHELL_BUS, SHELL_PATH, SHELL_BUS, "ShortcutStatus", None,
                                GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 1000, None)
        except GLib.Error:
            return None
        return json.loads(res.unpack()[0])

    # --- script file ------------------------------------------------------

    def load_file(self, path):
        path = str(Path(path).expanduser().resolve())
        try:
            text = Path(path).read_text(encoding="utf-8") if Path(path).exists() else ""
        except (OSError, UnicodeDecodeError) as e:
            self.win.toast(f"Не вдалося відкрити файл: {e}")
            return
        self.flush_save()
        self.path = path
        self.text = text
        self.index = self.settings["positions"].get(path, 0)
        self.settings["file"] = path
        self.save_settings()
        self.set_buffer_text(text)
        self.win.title.set_subtitle(Path(path).name)
        self.watch(path)
        self.update_blocks(reset_scroll=True)
        self.show_page_in_editor(place_cursor=True)

    def set_buffer_text(self, text):
        buffer = self.win.buffer
        cursor = buffer.get_iter_at_mark(buffer.get_insert()).get_offset()
        self.programmatic_cursor = True
        buffer.handler_block_by_func(self.on_buffer_changed)
        buffer.set_text(text)
        buffer.handler_unblock_by_func(self.on_buffer_changed)
        buffer.place_cursor(buffer.get_iter_at_offset(min(cursor, buffer.get_char_count())))
        self.programmatic_cursor = False
        self.tag_separators()

    def tag_separators(self):
        buffer = self.win.buffer
        buffer.remove_tag(self.win.separator_tag, buffer.get_start_iter(), buffer.get_end_iter())
        for m in SEPARATOR.finditer(self.text):
            buffer.apply_tag(self.win.separator_tag, buffer.get_iter_at_offset(m.start()),
                             buffer.get_iter_at_offset(m.end()))

    def on_cursor_moved(self, buffer, _pspec):
        # The overlay follows the page the user is editing.
        if not self.programmatic_cursor:
            self.go_to(block_at(self.text, buffer.props.cursor_position), from_editor=True)

    def show_page_in_editor(self, place_cursor=False):
        """Scrolls the editor to the current page (and puts the cursor there)."""
        spans = block_spans(self.text)
        if not spans:
            return
        buffer = self.win.buffer
        start = buffer.get_iter_at_offset(spans[self.index][0])
        if place_cursor:
            self.programmatic_cursor = True
            buffer.place_cursor(start)
            self.programmatic_cursor = False
        self.win.view.scroll_to_iter(start, 0.05, True, 0, 0.1)

    def on_buffer_changed(self, buffer):
        self.text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self.tag_separators()
        self.update_blocks()
        if self.save_id:
            GLib.source_remove(self.save_id)
        self.save_id = GLib.timeout_add(400, self.flush_save)

    def flush_save(self):
        if self.save_id:
            GLib.source_remove(self.save_id)
            self.save_id = 0
        if self.path:
            try:
                path = Path(self.path)
                # A new script file appears on disk with its first text.
                if (path.read_text(encoding="utf-8") if path.exists() else "") != self.text:
                    path.write_text(self.text, encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                self.win.toast(f"Не вдалося зберегти: {e}")
        return GLib.SOURCE_REMOVE

    def watch(self, path):
        if self.file_monitor:
            self.file_monitor.cancel()
        self.file_monitor = Gio.File.new_for_path(path).monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.file_monitor.connect("changed", self.on_file_changed)

    def on_file_changed(self, *_):
        # Editors save in several steps; read the file once things settle.
        if self.reload_id:
            GLib.source_remove(self.reload_id)
        self.reload_id = GLib.timeout_add(200, self.reload_from_disk)

    def reload_from_disk(self):
        self.reload_id = 0
        try:
            text = Path(self.path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return GLib.SOURCE_REMOVE
        if text != self.text and not self.save_id:
            self.text = text
            self.set_buffer_text(text)
            self.update_blocks()
        return GLib.SOURCE_REMOVE

    # --- display ------------------------------------------------------------

    def update_blocks(self, reset_scroll=False):
        self.blocks = split_blocks(self.text) or [""]
        self.index = max(0, min(self.index, len(self.blocks) - 1))
        self.render(reset_scroll)

    def render(self, reset_scroll=False):
        n = len(self.blocks)
        if self.index + 1 < n:
            preview = "→  " + first_line(self.blocks[self.index + 1])
        else:
            preview = "—  кінець  —"
        self.overlay.show_block(to_markup(self.blocks[self.index]),
                                f"{self.index + 1} / {n}   ·   {Path(self.path).name}",
                                preview, reset_scroll)
        self.win.position.set_text(f"Сторінка {self.index + 1} / {n}")
        buffer = self.win.buffer
        buffer.remove_tag(self.win.current_tag, buffer.get_start_iter(), buffer.get_end_iter())
        spans = block_spans(self.text)
        if spans:
            start, end = spans[self.index]
            buffer.apply_tag(self.win.current_tag, buffer.get_iter_at_offset(start),
                             buffer.get_iter_at_offset(end))

    def go_to(self, index, from_editor=False):
        if 0 <= index < len(self.blocks) and index != self.index:
            self.index = index
            self.settings["positions"][self.path] = index
            self.save_settings()
            self.render(reset_scroll=True)
            if not from_editor:
                self.show_page_in_editor()

    def update_css(self):
        s = self.settings
        self.css.load_from_string(f"""
            window.prompter-overlay {{ background: transparent; }}
            .prompter-overlay .card {{ background-color: rgba(0, 0, 0, {s["opacity"]:.2f});
                                       border-radius: 18px; padding: 16px 26px 12px; }}
            .prompter-overlay .text {{ color: #ffffff; font-size: {s["font_size"]}px; font-weight: 600;
                                       line-height: 1.3; text-shadow: 0 1px 4px rgba(0, 0, 0, 0.9); }}
            .prompter-overlay .status, .prompter-overlay .preview, .prompter-overlay .grip {{
                color: rgba(255, 255, 255, 0.6); font-size: 14px;
                text-shadow: 0 1px 3px rgba(0, 0, 0, 0.9); }}
            .editor {{ font-size: 16px; }}
        """)

    def update_hint(self):
        sc = self.shortcuts
        self.win.hint.set_label(f"{pretty_shortcut(sc['prev'])} / {pretty_shortcut(sc['next'])} сторінки   "
                                f"{pretty_shortcut(sc['toggle'])} суфлер   ⌨")

    def save_settings(self):
        save_json(SETTINGS_FILE, self.settings)

    def set_overlay_visible(self, visible):
        self.overlay_action.set_state(GLib.Variant.new_boolean(visible))
        self.settings["overlay_visible"] = visible
        self.save_settings()
        self.overlay.set_visible(visible)

    def set_locked(self, locked):
        self.locked_action.set_state(GLib.Variant.new_boolean(locked))
        self.settings["locked"] = locked
        self.save_settings()
        self.overlay.set_locked(locked)
        # Once GTK has committed the new input region, let the extension make
        # GNOME Shell re-pick what is under the pointer.
        GLib.timeout_add(200, self.refresh_shell_input)

    @staticmethod
    def refresh_shell_input():
        Gio.bus_get_sync(Gio.BusType.SESSION, None).call(
            SHELL_BUS, SHELL_PATH, SHELL_BUS, "RefreshInput", None, None,
            Gio.DBusCallFlags.NO_AUTO_START, 1000, None, None)
        return GLib.SOURCE_REMOVE

    def set_style(self, key, value):
        self.settings[key] = value
        self.save_settings()
        self.update_css()
        self.overlay.set_align(self.settings["align"])

    # --- actions (main window, overlay, CLI and global shortcuts) -----------

    def run_action(self, name):
        if self.pending_test == name and self.prefs:
            self.pending_test = None
            self.prefs["dialog"].add_toast(Adw.Toast(
                title=f"✓ {pretty_shortcut(self.shortcuts[name])} працює", timeout=3))
        getattr(self, "on_" + name.replace("-", "_"))()

    def on_main_close(self, _window):
        # Closing the editor keeps the app (overlay + global shortcuts) running.
        self.flush_save()
        self.win.set_visible(False)
        if not self.told_background:
            self.told_background = True
            note = Gio.Notification.new("Prompter працює у фоні")
            note.set_body(f"Гарячі клавіші працюють, {pretty_shortcut(self.shortcuts['toggle'])} — суфлер. "
                          "Відкрити редактор — «Prompter» у меню програм, вийти — меню → Вийти.")
            self.send_notification("background", note)
        return True

    def on_toggle(self):
        self.set_overlay_visible(not self.overlay_action.get_state().unpack())

    def on_show(self):
        self.set_overlay_visible(True)

    def on_hide(self):
        self.set_overlay_visible(False)

    def on_lock(self):
        self.set_locked(not self.locked_action.get_state().unpack())

    def on_next(self):
        self.go_to(self.index + 1)

    def on_prev(self):
        self.go_to(self.index - 1)

    def on_down(self):
        self.overlay.scroll(1, self.settings["font_size"] * 1.3 * 2)

    def on_up(self):
        self.overlay.scroll(-1, self.settings["font_size"] * 1.3 * 2)

    def on_bigger(self):
        self.set_style("font_size", min(160, self.settings["font_size"] + 4))

    def on_smaller(self):
        self.set_style("font_size", max(16, self.settings["font_size"] - 4))

    def on_new_page(self):
        buffer = self.win.buffer
        offset = page_break_offset(self.text, buffer.props.cursor_position)
        buffer.insert(buffer.get_iter_at_offset(offset), PAGE_BREAK)
        buffer.place_cursor(buffer.get_iter_at_offset(offset + len(PAGE_BREAK)))
        self.win.view.scroll_to_mark(buffer.get_insert(), 0.1, False, 0, 0)
        self.win.view.grab_focus()

    def on_reload(self):
        if self.path:
            self.load_file(self.path)

    def on_quit(self):
        self.flush_save()
        self.quit()

    def on_open(self):
        dialog = Gtk.FileDialog(title="Відкрити сценарій", filters=self.text_filters())
        dialog.open(self.win, None, self.on_open_done)

    def on_open_done(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        self.load_file(file.get_path())

    def on_save_as(self):
        dialog = Gtk.FileDialog(title="Зберегти сценарій як", filters=self.text_filters(),
                                initial_name=Path(self.path).name if self.path else "сценарій.md")
        dialog.save(self.win, None, self.on_save_as_done)

    def on_save_as_done(self, dialog, result):
        try:
            path = dialog.save_finish(result).get_path()
        except GLib.Error:
            return
        try:
            Path(path).write_text(self.text, encoding="utf-8")
        except OSError as e:
            self.win.toast(f"Не вдалося зберегти: {e}")
            return
        self.load_file(path)

    @staticmethod
    def text_filters():
        filters = Gio.ListStore.new(Gtk.FileFilter)
        text = Gtk.FileFilter(name="Текст і Markdown")
        for pattern in ("*.md", "*.txt", "*.markdown"):
            text.add_pattern(pattern)
        filters.append(text)
        return filters

    def on_about(self):
        Adw.AboutDialog(application_name="Prompter", application_icon=APP_ID, version=VERSION,
                        developer_name="Andrii",
                        comments="Суфлер поверх усіх вікон з глобальними гарячими клавішами").present(self.win)

    # --- preferences --------------------------------------------------------

    def on_shortcuts(self):
        self.on_preferences(page="keys")

    def on_preferences(self, page=None):
        self.win.present()
        dialog = Adw.PreferencesDialog(title="Налаштування")

        look = Adw.PreferencesPage(title="Вигляд", icon_name="applications-graphics-symbolic")
        group = Adw.PreferencesGroup(title="Суфлер")
        font = Adw.SpinRow.new_with_range(16, 160, 2)
        font.set_title("Розмір шрифту")
        font.set_value(self.settings["font_size"])
        font.connect("notify::value", lambda r, _p: self.set_style("font_size", int(r.get_value())))
        opacity = Adw.SpinRow.new_with_range(0, 100, 5)
        opacity.set_title("Непрозорість фону, %")
        opacity.set_subtitle("0 — видно лише текст, 100 — суцільний чорний фон")
        opacity.set_value(round(self.settings["opacity"] * 100))
        opacity.connect("notify::value", lambda r, _p: self.set_style("opacity", r.get_value() / 100))
        align = Adw.ComboRow(title="Вирівнювання тексту", model=Gtk.StringList.new(["Ліворуч", "По центру"]))
        align.set_selected(1 if self.settings["align"] == "center" else 0)
        align.connect("notify::selected", lambda r, _p:
                      self.set_style("align", "center" if r.get_selected() == 1 else "left"))
        for row in (font, opacity, align):
            group.add(row)
        look.add(group)
        dialog.add(look)

        keys = Adw.PreferencesPage(title="Клавіші", icon_name="input-keyboard-symbolic")
        group = Adw.PreferencesGroup(
            title="Гарячі клавіші",
            description="Клацніть на дію й натисніть свою комбінацію. Працюють з будь-якої програми, "
                        "навіть коли активний Zoom, поки Prompter запущений (закрите вікно редактора "
                        "не зупиняє програму).")
        rows, labels = {}, {}
        for name, _accel, title in ACTIONS:
            rows[name] = Adw.ActionRow(title=title, activatable=True)
            labels[name] = Gtk.Label(label=pretty_shortcut(self.shortcuts.get(name)))
            labels[name].add_css_class("dim-label")
            rows[name].add_suffix(labels[name])
            rows[name].add_suffix(Gtk.Image(icon_name="document-edit-symbolic"))
            rows[name].connect("activated", lambda _r, name=name: self.capture_shortcut(name))
            group.add(rows[name])
        reset = Gtk.Button(label="Типові", valign=Gtk.Align.CENTER)
        reset.add_css_class("flat")
        reset.connect("clicked", lambda _b: self.set_shortcuts(dict(DEFAULT_SHORTCUTS)))
        group.set_header_suffix(reset)
        keys.add(group)
        dialog.add(keys)
        if page == "keys":
            dialog.set_visible_page(keys)
        self.prefs = {"dialog": dialog, "rows": rows, "labels": labels}
        dialog.connect("closed", self.on_prefs_closed)
        dialog.present(self.win)
        self.show_shortcut_status()

    def on_prefs_closed(self, dialog):
        if self.prefs and self.prefs["dialog"] is dialog:
            self.prefs = None
            self.pending_test = None

    def publish_shortcuts(self, suspended=False):
        """Hands the shortcuts to the extension. While a new combination is being
        recorded, nothing is grabbed, so the dialog gets even combinations that
        are currently in use."""
        save_json(SHORTCUTS_FILE, {} if suspended else self.shortcuts)

    def set_shortcuts(self, shortcuts, changed=None):
        self.shortcuts = shortcuts
        self.settings["shortcuts"] = shortcuts
        self.save_settings()
        self.publish_shortcuts()
        self.update_hint()
        if self.prefs:
            for name, label in self.prefs["labels"].items():
                label.set_label(pretty_shortcut(shortcuts.get(name)))
        GLib.timeout_add(800, self.verify_shortcuts, changed)

    def show_shortcut_status(self, status=None):
        """Marks rows whose combination the extension could not grab."""
        status = status if status is not None else self.shortcut_status()
        if not self.prefs:
            return
        for name, row in self.prefs["rows"].items():
            taken = status is not None and self.shortcuts.get(name) and status.get(name) is False
            row.set_subtitle("Зайнята іншою програмою — оберіть іншу" if taken else "")
            (row.add_css_class if taken else row.remove_css_class)("error")

    def verify_shortcuts(self, changed):
        status = self.shortcut_status()
        self.show_shortcut_status(status)
        if not self.prefs or status is None:
            return GLib.SOURCE_REMOVE
        accel = pretty_shortcut(self.shortcuts.get(changed)) if changed else None
        if changed and status.get(changed) is False:
            self.prefs["dialog"].add_toast(Adw.Toast(title=f"{accel} уже зайнята системою — оберіть іншу", timeout=4))
        elif changed and self.shortcuts.get(changed):
            self.pending_test = changed
            self.prefs["dialog"].add_toast(Adw.Toast(title=f"Збережено. Натисніть {accel}, щоб перевірити", timeout=4))
        return GLib.SOURCE_REMOVE

    def capture_shortcut(self, name):
        title = next(label for n, _a, label in ACTIONS if n == name)
        dialog = Adw.AlertDialog(heading=title,
                                 body="Натисніть нову комбінацію, наприклад ⌃⌘→, або клавішу клікера PgDn.")
        current = self.shortcuts.get(name)
        label = Gtk.Label(label=pretty_shortcut(current))
        label.add_css_class("title-1")
        error = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(label)
        box.append(error)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Скасувати")
        dialog.add_response("clear", "Прибрати")
        dialog.add_response("save", "Зберегти")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_response_enabled("clear", bool(current))
        dialog.set_response_enabled("save", False)
        dialog.set_close_response("cancel")
        captured = {"accel": None}

        def on_key(_ctl, keyval, keycode, state):
            mods = state & Gtk.accelerator_get_default_mod_mask()
            # Record the key as it is on the first keyboard layout, so a
            # shortcut captured in the Ukrainian layout works in any layout.
            ok, base, *_ = self.win.get_display().translate_key(keycode, 0, 0)
            if ok:
                keyval = base
            keyval = Gdk.keyval_to_lower(keyval)
            key = Gdk.keyval_name(keyval) or ""
            if key.startswith(("Control", "Alt", "Super", "Shift", "Meta", "ISO_Level")):
                return False  # a modifier alone: wait for the real key
            if keyval == Gdk.KEY_Escape and not mods:
                return False  # let the dialog close
            accel = Gtk.accelerator_name(keyval, mods)
            err = validate_shortcut(accel, self.shortcuts, name)
            label.set_label(pretty_shortcut(accel))
            note = err or ("Без модифікатора: поки працює Prompter, ця клавіша не працюватиме "
                           "в інших програмах." if is_standalone(accel) else "")
            error.set_label(note)
            (error.add_css_class if err else error.remove_css_class)("error")
            captured["accel"] = None if err else accel
            dialog.set_response_enabled("save", err is None)
            return True

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", on_key)
        dialog.add_controller(keys)

        def on_response(_d, response):
            if response == "save" and captured["accel"]:
                self.set_shortcuts({**self.shortcuts, name: captured["accel"]}, name)
            elif response == "clear":
                self.set_shortcuts({**self.shortcuts, name: ""}, name)
            else:
                self.publish_shortcuts()

        dialog.connect("response", on_response)
        self.publish_shortcuts(suspended=True)
        dialog.present(self.prefs["dialog"] if self.prefs else self.win)
        return dialog


# --- CLI ---------------------------------------------------------------------

def send_action(name):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    try:
        bus.call_sync(APP_ID, APP_PATH, "org.gtk.Actions", "Activate",
                      GLib.Variant("(sava{sv})", (name, [], {})),
                      None, Gio.DBusCallFlags.NO_AUTO_START, 1000, None)
    except GLib.Error:
        print("Prompter не запущено.", file=sys.stderr)
        return 1
    return 0


USAGE = f"""\
Використання:
  prompter [ФАЙЛ]   запустити / відкрити сценарій
  prompter ДІЯ      надіслати дію запущеному суфлеру
                    ({", ".join(REMOTE_ACTIONS)})
"""


def main(argv):
    arg = argv[1] if len(argv) > 1 else None
    if arg in ("-h", "--help"):
        print(USAGE, end="")
        return 0
    if arg in REMOTE_ACTIONS and not Path(arg).exists():
        return send_action(arg)
    return Prompter().run(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
