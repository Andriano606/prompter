// Prompter backend: keeps the prompter overlay above all windows and on every
// workspace, and turns global shortcuts into app actions. Wayland clients can
// do neither themselves, so this runs inside GNOME Shell.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const APP_ID = 'org.andrii.Prompter';
const APP_PATH = '/org/andrii/Prompter';
// Must match OVERLAY_TITLE in prompter.py.
const OVERLAY_TITLE = 'Prompter — суфлер';
const BUS_NAME = 'org.andrii.Prompter.Shell';
const OBJECT_PATH = '/org/andrii/Prompter/Shell';

const IFACE_XML = `
<node>
  <interface name="org.andrii.Prompter.Shell">
    <method name="ShortcutStatus">
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="RefreshInput"/>
  </interface>
</node>`;

function configFile(name) {
    return Gio.File.new_for_path(GLib.build_filenamev([GLib.get_user_config_dir(), 'prompter', name]));
}

function readJson(file) {
    try {
        const [, bytes] = file.load_contents(null);
        return JSON.parse(new TextDecoder().decode(bytes));
    } catch {
        return null;
    }
}

function writeJson(file, data) {
    try {
        GLib.mkdir_with_parents(file.get_parent().get_path(), 0o755);
        file.replace_contents(new TextEncoder().encode(JSON.stringify(data)), null, false,
            Gio.FileCreateFlags.REPLACE_DESTINATION, null);
    } catch (e) {
        console.warn(`prompter: cannot write ${file.get_path()}: ${e.message}`);
    }
}

// Runs a GAction of the app over D-Bus (org.gtk.Actions). "toggle" starts the
// app when it isn't running.
function activateAppAction(name) {
    Gio.DBus.session.call(APP_ID, APP_PATH, 'org.gtk.Actions', 'Activate',
        new GLib.Variant('(sava{sv})', [name, [], {}]), null,
        Gio.DBusCallFlags.NO_AUTO_START, -1, null, (conn, res) => {
            try {
                conn.call_finish(res);
            } catch {
                if (name === 'toggle')
                    Shell.AppSystem.get_default().lookup_app(`${APP_ID}.desktop`)?.activate();
            }
        });
}

// Global keyboard shortcuts from ~/.config/prompter/shortcuts.json
// ({action: "<Control><Super>Right", ...}), written by the app.
class Shortcuts {
    constructor() {
        this._file = configFile('shortcuts.json');
        this._actions = new Map(); // keybinding action -> app action name
        this._status = {};
        this._debounceId = 0;
        this._signalId = global.display.connect('accelerator-activated', (_display, action) => {
            const name = this._actions.get(action);
            if (name)
                activateAppAction(name);
        });
        GLib.mkdir_with_parents(this._file.get_parent().get_path(), 0o755);
        // Monitor the directory: the app saves via rename, which a file
        // monitor on shortcuts.json itself can miss.
        this._monitor = this._file.get_parent().monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, null);
        this._monitor.connect('changed', (_m, file, other) => {
            if (![file?.get_basename(), other?.get_basename()].includes('shortcuts.json'))
                return;
            if (this._debounceId)
                GLib.source_remove(this._debounceId);
            this._debounceId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 150, () => {
                this._debounceId = 0;
                this.reload();
                return GLib.SOURCE_REMOVE;
            });
        });
        this.reload();
    }

    _ungrabAll() {
        for (const action of this._actions.keys())
            global.display.ungrab_accelerator(action);
        this._actions.clear();
    }

    reload() {
        this._ungrabAll();
        this._status = {};
        for (const [name, accel] of Object.entries(readJson(this._file) ?? {})) {
            if (!accel)
                continue;
            const action = global.display.grab_accelerator(accel, Meta.KeyBindingFlags.NONE);
            this._status[name] = action !== Meta.KeyBindingAction.NONE;
            if (action === Meta.KeyBindingAction.NONE) {
                console.warn(`prompter: cannot grab ${accel} (already in use?)`);
                continue;
            }
            Main.wm.allowKeybinding(Meta.external_binding_name_for_action(action),
                Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW);
            this._actions.set(action, name);
        }
    }

    status() {
        return this._status;
    }

    destroy() {
        if (this._debounceId)
            GLib.source_remove(this._debounceId);
        this._monitor.cancel();
        global.display.disconnect(this._signalId);
        this._ungrabAll();
    }
}

// Keeps the overlay window above others and on all workspaces, gives focus
// back to whatever had it when the overlay appears, and remembers the
// overlay's geometry in ~/.config/prompter/overlay.json. Hiding the overlay
// unmaps it, so every show is a new Meta.Window and goes through _adopt().
class OverlayKeeper {
    constructor() {
        this._file = configFile('overlay.json');
        this._window = null;
        this._windowSignals = [];
        this._timeouts = new Set();
        this._saveId = 0;
        this._restoring = false;
        this._createdId = global.display.connect('window-created', (_d, win) => {
            const previousFocus = global.display.focus_window;
            const id = win.connect('shown', () => {
                win.disconnect(id);
                if (this._isOverlay(win))
                    this._adopt(win, previousFocus);
            });
        });
        // A focused fullscreen window shares the "above" layer; stay on top of it.
        this._focusId = global.display.connect('notify::focus-window', () => {
            if (this._window && !this._window.minimized && global.display.focus_window !== this._window)
                this._window.raise();
        });
        for (const actor of global.get_window_actors()) {
            if (this._isOverlay(actor.meta_window))
                this._adopt(actor.meta_window, null);
        }
    }

    _isOverlay(win) {
        return win.get_gtk_application_id() === APP_ID && win.get_title() === OVERLAY_TITLE;
    }

    _later(ms, fn) {
        const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._timeouts.delete(id);
            fn();
            return GLib.SOURCE_REMOVE;
        });
        this._timeouts.add(id);
        return id;
    }

    _savedRect() {
        const r = readJson(this._file);
        if (!r || ![r.x, r.y, r.width, r.height].every(Number.isFinite))
            return null;
        // Skip a rect whose centre is off every monitor (layout changed).
        const cx = r.x + r.width / 2;
        const cy = r.y + r.height / 2;
        for (let i = 0; i < global.display.get_n_monitors(); i++) {
            const m = global.display.get_monitor_geometry(i);
            if (cx >= m.x && cx < m.x + m.width && cy >= m.y && cy < m.y + m.height)
                return r;
        }
        return null;
    }

    _adopt(win, previousFocus) {
        this._release();
        this._window = win;
        win.make_above();
        win.stick();

        const r = this._savedRect();
        if (r) {
            // Wayland resizes are async and a freshly mapped client may ignore
            // the first configure: apply, then re-check once.
            this._restoring = true;
            win.move_resize_frame(true, r.x, r.y, r.width, r.height);
            this._later(300, () => {
                if (this._window !== win)
                    return;
                const f = win.get_frame_rect();
                if (Math.abs(f.x - r.x) > 4 || Math.abs(f.y - r.y) > 4 ||
                    Math.abs(f.width - r.width) > 4 || Math.abs(f.height - r.height) > 4)
                    win.move_resize_frame(true, r.x, r.y, r.width, r.height);
                this._later(300, () => {
                    this._restoring = false;
                });
            });
        }

        if (previousFocus && previousFocus !== win && global.display.focus_window === win &&
            !previousFocus.minimized)
            previousFocus.activate(global.get_current_time());

        this._windowSignals = [
            win.connect('position-changed', () => this._scheduleSave()),
            win.connect('size-changed', () => this._scheduleSave()),
            win.connect('unmanaged', () => this._release()),
        ];
    }

    // Clutter keeps the actor under the pointer cached until the next relayout,
    // and a changed input region doesn't cause one: without this, clicks keep
    // going to the old target until the pointer leaves the overlay.
    refreshInput() {
        this._window?.get_compositor_private()?.queue_relayout();
    }

    _scheduleSave() {
        if (this._restoring)
            return;
        if (this._saveId)
            GLib.source_remove(this._saveId);
        this._timeouts.delete(this._saveId);
        this._saveId = this._later(400, () => {
            this._saveId = 0;
            if (!this._window)
                return;
            const f = this._window.get_frame_rect();
            writeJson(this._file, {x: f.x, y: f.y, width: f.width, height: f.height});
        });
    }

    _release() {
        if (this._window) {
            for (const id of this._windowSignals)
                this._window.disconnect(id);
        }
        this._windowSignals = [];
        this._window = null;
        this._restoring = false;
    }

    destroy() {
        for (const id of this._timeouts)
            GLib.source_remove(id);
        this._timeouts.clear();
        global.display.disconnect(this._createdId);
        global.display.disconnect(this._focusId);
        this._release();
    }
}

class Backend {
    constructor(shortcuts, overlay) {
        this._shortcuts = shortcuts;
        this._overlay = overlay;
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._ownerId = Gio.bus_own_name_on_connection(
            Gio.DBus.session, BUS_NAME, Gio.BusNameOwnerFlags.REPLACE, null, null);
    }

    // {action: true|false} — whether the action's shortcut could be grabbed.
    ShortcutStatus() {
        return JSON.stringify(this._shortcuts.status());
    }

    // Called by the app after it locks/unlocks the overlay (input region change).
    RefreshInput() {
        this._overlay.refreshInput();
    }

    destroy() {
        Gio.bus_unown_name(this._ownerId);
        this._dbus.unexport();
        this._dbus = null;
    }
}

export default class PrompterExtension extends Extension {
    enable() {
        this._shortcuts = new Shortcuts();
        this._overlay = new OverlayKeeper();
        this._backend = new Backend(this._shortcuts, this._overlay);
    }

    disable() {
        this._backend?.destroy();
        this._backend = null;
        this._overlay?.destroy();
        this._overlay = null;
        this._shortcuts?.destroy();
        this._shortcuts = null;
    }
}
