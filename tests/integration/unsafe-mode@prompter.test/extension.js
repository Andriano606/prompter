// Test-only: lets tests/integration/scenario.py inspect windows and
// inject key presses through org.gnome.Shell.Eval in the headless shell.
export default class UnsafeMode {
    enable() {
        global.context.unsafe_mode = true;
    }

    disable() {
        global.context.unsafe_mode = false;
    }
}
