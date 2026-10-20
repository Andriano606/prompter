import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import prompter as pr  # noqa: E402


def test_split_blocks_on_dashes_and_skips_empty():
    text = "Перший\nрядок\n---\n\n  \n----\nДругий\n  ---  \nТретій\n"
    assert pr.split_blocks(text) == ["Перший\nрядок", "Другий", "Третій"]


def test_split_blocks_keeps_inline_dashes():
    assert pr.split_blocks("a --- b\n-- c") == ["a --- b\n-- c"]


def test_markup_escapes_and_formats():
    markup = pr.to_markup("# Заголовок\n**жирний** <тег> & ще\n// примітка <x>")
    lines = markup.split("\n")
    assert lines[0] == "<b>Заголовок</b>"
    assert lines[1] == "<b>жирний</b> &lt;тег&gt; &amp; ще"
    assert lines[2] == '<span alpha="70%"><small><small>примітка &lt;x&gt;</small></small></span>'


def test_first_line_skips_notes_and_markup():
    assert pr.first_line("// нотатка\n\n# **Привіт**, світ") == "Привіт, світ"
    assert pr.first_line("// лише нотатка") == ""


def test_pretty_shortcut_uses_mac_symbols():
    assert pr.pretty_shortcut("<Control><Super>Right") == "⌃⌘→"
    assert pr.pretty_shortcut("<Super><Control>h") == "⌃⌘H"
    assert pr.pretty_shortcut("<Alt><Shift>minus") == "⌥⇧−"
    assert pr.pretty_shortcut("") == "—"


def test_validate_shortcut():
    shortcuts = dict(pr.DEFAULT_SHORTCUTS)
    assert pr.validate_shortcut("h", shortcuts, "toggle").startswith("Потрібен модифікатор")
    # Same keys in another order count as a duplicate.
    err = pr.validate_shortcut("<Super><Control>Right", shortcuts, "toggle")
    assert err and "Наступна сторінка" in err
    assert pr.validate_shortcut("<Control><Super>Right", shortcuts, "next") is None
    assert pr.validate_shortcut("<Control><Super>k", shortcuts, "toggle") is None


def test_defaults_have_unique_shortcuts():
    normalized = [pr.normalize_shortcut(a) for a in pr.DEFAULT_SHORTCUTS.values()]
    assert len(set(normalized)) == len(normalized)


def test_load_json_merges_defaults_and_survives_garbage(tmp_path):
    path = tmp_path / "s.json"
    assert pr.load_json(path, {"a": 1}) == {"a": 1}
    path.write_text("not json")
    assert pr.load_json(path, {"a": 1}) == {"a": 1}
    path.write_text(json.dumps({"b": 2}))
    assert pr.load_json(path, {"a": 1}) == {"a": 1, "b": 2}
    path.write_text("[1, 2]")
    assert pr.load_json(path, {"a": 1}) == {"a": 1}


def test_save_json_is_atomic_and_readable(tmp_path):
    path = tmp_path / "dir" / "s.json"
    pr.save_json(path, {"текст": "так"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"текст": "так"}
    assert not path.with_suffix(".tmp").exists()


def test_remote_actions_have_handlers():
    for name in pr.REMOTE_ACTIONS:
        assert hasattr(pr.Prompter, "on_" + name.replace("-", "_")), name


def test_window_actions_are_cli_actions_not_file_names():
    # `prompter preferences` must open the dialog, not a script named "preferences".
    for name in ("preferences", "about", "open", "save-as", "show", "hide"):
        assert name in pr.REMOTE_ACTIONS


def test_block_at_maps_cursor_to_page():
    text = "A1\nA2\n---\n\n---\nB\n---\nC"
    assert [text[s:e].strip() for s, e in pr.block_spans(text)] == ["A1\nA2", "B", "C"]
    assert pr.block_at(text, 0) == 0
    assert pr.block_at(text, text.index("A2") + 2) == 0
    assert pr.block_at(text, text.index("B")) == 1
    assert pr.block_at(text, text.index("C") + 1) == 2
    assert pr.block_at(text, len(text)) == 2
    assert pr.block_at("", 0) == 0


def test_new_page_goes_right_after_the_current_page():
    text = "Перша\n\n\n---\nДруга\n"
    offset = pr.page_break_offset(text, 1)  # cursor in "Перша"
    new = text[:offset] + pr.PAGE_BREAK + text[offset:]
    cursor = offset + len(pr.PAGE_BREAK)
    typed = new[:cursor] + "Нова" + new[cursor:]
    assert pr.split_blocks(typed) == ["Перша", "Нова", "Друга"]
    # At the end of the script the new page is appended.
    end = pr.page_break_offset(text, len(text))
    assert pr.split_blocks(text[:end] + pr.PAGE_BREAK + "Третя") == ["Перша", "Друга", "Третя"]
    assert pr.page_break_offset("", 0) == 0


def test_resize_edge():
    w, h = 800, 300
    assert pr.resize_edge(400, 150, w, h) is None
    assert pr.resize_edge(2, 150, w, h) == "WEST"
    assert pr.resize_edge(799, 150, w, h) == "EAST"
    assert pr.resize_edge(400, 0, w, h) == "NORTH"
    assert pr.resize_edge(400, 299, w, h) == "SOUTH"
    assert pr.resize_edge(795, 295, w, h) == "SOUTH_EAST"
    assert pr.resize_edge(3, 3, w, h) == "NORTH_WEST"
    for edge in ("NORTH", "SOUTH_EAST", "NORTH_WEST", "EAST"):
        assert hasattr(pr.Gdk.SurfaceEdge, edge) and edge in pr.RESIZE_CURSORS


def test_standalone_keys_allowed_without_modifier():
    shortcuts = dict(pr.DEFAULT_SHORTCUTS)
    assert pr.validate_shortcut("Page_Down", shortcuts, "next") is None
    assert pr.validate_shortcut("F5", shortcuts, "toggle") is None
    assert pr.validate_shortcut("a", shortcuts, "next").startswith("Потрібен модифікатор")
    assert pr.validate_shortcut("Right", shortcuts, "next").startswith("Потрібен модифікатор")
    assert pr.is_standalone("Page_Up") and not pr.is_standalone("<Control>Page_Up")
    assert pr.pretty_shortcut("Page_Down") == "PgDn"


def test_links_become_buttons():
    block = "Дивіться [Демо](example.com/demo?a=1&b=2) і [Док](https://docs.io)\nтекст"
    assert pr.links_of(block) == [("Демо", "https://example.com/demo?a=1&b=2"), ("Док", "https://docs.io")]
    assert pr.links_of("без посилань [не](посилання") == []
    assert pr.normalize_url(" mailto:a@b.c ") == "mailto:a@b.c"


def test_link_markup_keeps_title_underlined_and_escapes():
    assert pr.to_markup("**Увага** [A & B](x.com/?q=1&r=2)") == "<b>Увага</b> <u>A &amp; B</u>"
    assert pr.first_line("[Демо](example.com) — початок") == "Демо — початок"
    # A line with nothing but a link becomes only a button.
    assert pr.to_markup("Текст\n  [Демо](example.com)  \nдалі") == "Текст\nдалі"


TABLE_SCRIPT = r"""# Сценарій / Script
> Примітка над таблицею.

| Українська | English |
|---|---|
| **1. Вступ**<br>Привіт! | **1. Intro**<br>Hi! |
| **2. Демо**<br>[Відкрити X → Y]<br>• пункт | **2. Demo**<br>[Open X → Y] |
| з \| трубою | with \| pipe |

---

Після таблиці
"""


def test_table_rows_become_pages_in_one_column():
    assert pr.table_columns(TABLE_SCRIPT) == ["Українська", "English"]
    uk = pr.split_blocks(TABLE_SCRIPT, 0)
    assert uk == ["# Сценарій / Script\n> Примітка над таблицею.", "**1. Вступ**\nПривіт!",
                  "**2. Демо**\n[Відкрити X → Y]\n• пункт", "з | трубою", "Після таблиці"]
    en = pr.split_blocks(TABLE_SCRIPT, 1)
    assert en[1:4] == ["**1. Intro**\nHi!", "**2. Demo**\n[Open X → Y]", "with | pipe"]
    assert pr.table_columns("без таблиць | тут") == []


def test_table_row_spans_and_new_page_after_the_table():
    pages = pr.parse_pages(TABLE_SCRIPT)
    row = pages[1]
    assert TABLE_SCRIPT[row.start:row.end].startswith("| **1. Вступ**") and "\n" not in TABLE_SCRIPT[row.start:row.end]
    assert pr.block_at(TABLE_SCRIPT, row.start + 3) == 1
    # A new page from inside the table goes after the table, keeping it intact.
    offset = pr.page_break_offset(TABLE_SCRIPT, row.start + 3)
    assert TABLE_SCRIPT[:offset].endswith("| з \\| трубою | with \\| pipe |")
    new = TABLE_SCRIPT[:offset] + pr.PAGE_BREAK + "Нова" + TABLE_SCRIPT[offset:]
    assert pr.split_blocks(new)[4:] == ["Нова", "Після таблиці"]


def test_markdown_rendering_extras():
    markup = pr.to_markup("**2. Демо**<br>[Відкрити X → Y]\n- пункт *курсив* `код`\n> цитата")
    assert markup.split("\n") == [
        "<b>2. Демо</b>",
        '<span alpha="60%"><i><small>[Відкрити X → Y]</small></i></span>',
        "• пункт <i>курсив</i> <tt>код</tt>",
        '<span alpha="70%"><small><small>цитата</small></small></span>',
    ]
    assert pr.to_markup("2 * 3 * 4") == "2 * 3 * 4"
    assert pr.first_line("> нотатка\n- пункт") == "• пункт"
