from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TUI = (ROOT / "bench-client" / "vstl-imaging-tui.py").read_text(encoding="utf-8")


class DisplayTypePauseTests(unittest.TestCase):
    def test_display_type_screen_holds_three_seconds_with_enter_skip(self):
        self.assertIn("Touch test skipped. Moving to the next QC step in 3 seconds.", TUI)
        self.assertIn("Opening full-screen touch map in 3 seconds.", TUI)
        self.assertIn(
            'draw_footer(stdscr, "Continuing in 3 seconds   ENTER skip   T force touch test")',
            TUI,
        )
        self.assertIn('draw_footer(stdscr, "Touch test starts in 3 seconds   ENTER skip")', TUI)
        self.assertIn("_wait_with_skip(stdscr, 3)", TUI)
        self.assertNotIn("time.sleep(1.2)", TUI)


if __name__ == "__main__":
    unittest.main()
