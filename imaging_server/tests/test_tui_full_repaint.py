from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TUI = (ROOT / "bench-client" / "vstl-imaging-tui.py").read_text(encoding="utf-8")


class TuiFullRepaintTests(unittest.TestCase):
    def test_draw_header_prepares_transition_without_hot_loop_clear(self):
        self.assertIn("def _write_tty_bytes", TUI)
        self.assertIn("def _prepare_screen_transition", TUI)
        self.assertIn("def _physical_blank_tty", TUI)
        self.assertIn("def _blank_canvas", TUI)
        self.assertIn('for path in ("/dev/tty", "/dev/tty1", "/dev/console"):', TUI)
        self.assertIn('os.open(path, os.O_WRONLY | os.O_NOCTTY)', TUI)
        self.assertIn('b"\\033[?25l\\033[0m\\033[H\\033[2J\\033[3J"', TUI)
        self.assertIn('_write_tty_bytes(b"\\033[?25l\\033[0m\\033[H\\033[2J\\033[3J")', TUI)
        self.assertIn('chunks.append(f"\\033[{y};1H".encode("ascii"))', TUI)
        self.assertIn('_write_tty_bytes(b"".join(chunks))', TUI)
        self.assertIn("stdscr.clear()", TUI)
        self.assertIn("stdscr.clearok(True)", TUI)
        self.assertIn('stdscr.bkgd(" ", curses.A_NORMAL)', TUI)
        self.assertIn("stdscr.touchwin()", TUI)
        self.assertNotIn("curses.flushinp()", TUI)
        self.assertIn('blank = " " * width', TUI)
        self.assertIn("stdscr.clrtoeol()", TUI)
        self.assertIn("stdscr.addstr(y, 0, blank, curses.A_NORMAL)", TUI)
        self.assertNotIn("def _force_full_repaint", TUI)
        self.assertIn("stdscr.redrawwin()", TUI)
        self.assertIn("curses.doupdate()", TUI)
        transition_pos = TUI.index("_prepare_screen_transition(stdscr, subtitle)")
        blank_pos = TUI.index("_blank_canvas(stdscr)")
        title_pos = TUI.index('title = " VSTL 360')
        self.assertLess(transition_pos, title_pos)
        self.assertLess(blank_pos, title_pos)

    def test_screen_transition_drains_input_between_local_layer_and_menu(self):
        self.assertIn("def _begin_screen_frame", TUI)
        self.assertIn("def _drain_pending_input", TUI)
        self.assertIn('_begin_screen_frame(stdscr, "Select working layer")', TUI)
        self.assertIn('_begin_screen_frame(stdscr, f"Technician: {technician}")', TUI)
        self.assertIn("selected_layer = screen_working_layer(stdscr, _operator_name(operator))", TUI)
        self.assertIn("tech = _operator_technician_level(operator)", TUI)
        layer_pos = TUI.index("selected_layer = screen_working_layer(stdscr, _operator_name(operator))")
        drain_pos = TUI.index("_drain_pending_input(stdscr)", layer_pos)
        menu_pos = TUI.index("choice = screen_main_menu(stdscr, tech, operator)")
        self.assertLess(layer_pos, drain_pos)
        self.assertLess(drain_pos, menu_pos)

    def test_selectable_rows_are_fixed_width_ascii(self):
        self.assertIn("def _draw_selectable_row", TUI)
        self.assertIn('marker: str = ">"', TUI)
        self.assertIn('_safe_addstr(stdscr, y, 0, " " * width, curses.A_NORMAL)', TUI)
        self.assertIn("curses.color_pair(GREEN_PAIR) if selected else curses.A_NORMAL", TUI)
        self.assertIn("row[:text_width]", TUI)
        self.assertIn('draw_footer(stdscr, f"UP/DOWN navigate', TUI)
        self.assertNotIn('marker = "▶"', TUI)
        self.assertNotIn("curses.A_REVERSE | curses.A_BOLD if selected", TUI)

    def test_multiline_center_text_and_non_reverse_chrome(self):
        self.assertIn("parts = str(text).splitlines() or", TUI)
        self.assertIn("for part in parts:", TUI)
        self.assertIn("curses.init_pair(PURPLE_PAIR, curses.COLOR_WHITE, curses.COLOR_MAGENTA)", TUI)
        self.assertIn("curses.init_pair(FOOTER_PAIR, curses.COLOR_BLACK, curses.COLOR_WHITE)", TUI)
        self.assertNotIn("curses.color_pair(PURPLE_PAIR) | curses.A_REVERSE", TUI)
        self.assertIn("stdscr.keypad(True)", TUI)
        self.assertIn("stdscr.leaveok(True)", TUI)


if __name__ == "__main__":
    unittest.main()
