from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENTRY = (ROOT / "bench-client" / "vstl-bench-entry.sh").read_text(encoding="utf-8")
TUI = (ROOT / "bench-client" / "vstl-imaging-tui.py").read_text(encoding="utf-8")


class BenchEntryCleanTuiStartTests(unittest.TestCase):
    def test_entry_waits_and_clears_tty_before_tui(self):
        self.assertIn("wait_for_console_quiet", ENTRY)
        self.assertIn("VSTL_TUI_START_DELAY_SEC", ENTRY)
        self.assertIn("clear_tui_tty", ENTRY)
        self.assertIn("activate_tui_tty", ENTRY)
        wait_pos = ENTRY.index("wait_for_console_quiet")
        tui_pos = ENTRY.index('python3 "$TUI_PATH"')
        self.assertLess(wait_pos, tui_pos)

    def test_entry_uses_visible_operator_vt(self):
        self.assertIn('TUI_TTY="${VSTL_TUI_TTY:-/dev/tty1}"', ENTRY)
        self.assertIn('TUI_VT="${VSTL_TUI_VT:-${TUI_TTY#/dev/tty}}"', ENTRY)
        self.assertIn('chvt "$TUI_VT"', ENTRY)
        self.assertIn("same tty requested by ocs_live_run", ENTRY)
        self.assertIn("Preparing VSTL bench screen", ENTRY)

    def test_entry_hard_resets_operator_tty_before_curses(self):
        self.assertIn("stty sane", ENTRY)
        self.assertIn("printf '\\033c\\033[?25h\\033[H\\033[2J\\033[3J'", ENTRY)

    def test_entry_disables_console_blanking_before_curses(self):
        self.assertIn("keep_operator_console_awake", ENTRY)
        self.assertIn("/sys/module/kernel/parameters/consoleblank", ENTRY)
        self.assertIn("setterm --blank 0 --powerdown 0 --powersave off", ENTRY)
        keepalive_pos = ENTRY.index("keep_operator_console_awake")
        tui_pos = ENTRY.index('python3 "$TUI_PATH"')
        self.assertLess(keepalive_pos, tui_pos)

    def test_entry_mutes_kernel_console_before_curses(self):
        self.assertIn("quiet_kernel_console", ENTRY)
        self.assertIn("dmesg -D", ENTRY)
        self.assertIn("/proc/sys/kernel/printk", ENTRY)
        quiet_pos = ENTRY.index("quiet_kernel_console")
        tui_pos = ENTRY.index('python3 "$TUI_PATH"')
        self.assertLess(quiet_pos, tui_pos)

    def test_entry_applies_large_console_font(self):
        self.assertIn("apply_console_font", ENTRY)
        self.assertIn("VSTL_CONSOLE_FONT", ENTRY)
        self.assertIn("TerminusBold32x16", ENTRY)
        activate = ENTRY[ENTRY.index("activate_tui_tty()"):ENTRY.index("wait_for_console_quiet()")]
        font_pos = activate.index("apply_console_font")
        clear_pos = activate.index("clear_tui_tty")
        self.assertLess(font_pos, clear_pos)

    def test_network_setup_runs_on_operator_tty_before_tui(self):
        self.assertIn('NETWORK_SETUP_PATH="/opt/vstl/vstl_network_setup.py"', ENTRY)
        self.assertIn("run_network_setup", ENTRY)
        self.assertIn('python3 "$NETWORK_SETUP_PATH"', ENTRY)
        network_pos = ENTRY.index("run_network_setup ||")
        tui_pos = ENTRY.index('python3 "$TUI_PATH"')
        self.assertLess(network_pos, tui_pos)

    def test_entry_allows_only_one_console_owner(self):
        self.assertIn('ENTRY_LOCK="${VSTL_ENTRY_LOCK:-/run/vstl-bench-entry.lock}"', ENTRY)
        self.assertIn("take_single_instance_lock", ENTRY)
        self.assertIn("flock -n 9", ENTRY)
        self.assertIn("duplicate launcher will wait", ENTRY)
        self.assertIn("wait_for_existing_instance", ENTRY)
        self.assertIn("cleanup_single_instance_lock", ENTRY)
        lock_pos = ENTRY.index("if ! take_single_instance_lock; then")
        tui_pos = ENTRY.index('python3 "$TUI_PATH"')
        self.assertLess(lock_pos, tui_pos)

    def test_successful_completion_reboots_instead_of_powering_off(self):
        case_start = ENTRY.index('case "$TUI_RC" in')
        debug_case = ENTRY.index("\n    2)", case_start)
        completion_block = ENTRY[case_start:debug_case]
        self.assertIn("restarting system now", completion_block)
        self.assertIn("systemctl reboot -i", completion_block)
        self.assertIn("shutdown -r now", completion_block)
        self.assertIn("reboot -f", completion_block)
        self.assertNotIn("poweroff", completion_block)
        self.assertNotIn("shutdown -h now", completion_block)

    def test_completion_footer_prompts_restart(self):
        self.assertIn('draw_footer(stdscr, "ENTER restart system   Q drop to shell")', TUI)
        self.assertNotIn('draw_footer(stdscr, "ENTER power off   Q drop to shell")', TUI)

    def test_debug_and_failure_paths_open_recovery_shell_instead_of_live_shutdown(self):
        self.assertIn("open_recovery_shell_or_hold", ENTRY)
        self.assertIn("VSTL recovery shell", ENTRY)
        self.assertIn('exec /bin/bash -li <"$TUI_TTY" >"$TUI_TTY" 2>&1', ENTRY)
        self.assertIn('open_recovery_shell_or_hold "TUI requested debug shell', ENTRY)
        self.assertIn('open_recovery_shell_or_hold "TUI failed unexpectedly', ENTRY)
        debug_case = ENTRY[ENTRY.index("\n    2)"):ENTRY.index("\n    *)")]
        failure_case = ENTRY[ENTRY.index("\n    *)"):ENTRY.index("\nesac")]
        self.assertNotIn('exit "$TUI_RC"', debug_case)
        self.assertNotIn('exit "$TUI_RC"', failure_case)


if __name__ == "__main__":
    unittest.main()
