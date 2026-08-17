#!/usr/bin/env bash
# =============================================================================
# vstl-bench-entry.sh — Phase-1 bench entrypoint
# =============================================================================
# This is what Clonezilla's `ocs_live_run` kernel param now points at (set by
# 04_build_live_iso_clonezilla.sh).  It does the minimum work the Python TUI
# can't — bring up wired/USB Ethernet or Wi-Fi, then hand control to the TUI.
#
# Why a shell wrapper at all (vs. running the TUI directly from systemd)?
# 1. Clonezilla Live boots before networking is guaranteed. The network helper
#    tries every physical Ethernet device (including USB adapters), then offers
#    a Wi-Fi selector when the VSTL server is still unreachable.
# 2. We want a single recovery path: if the TUI crashes (Python exception,
#    missing tty, etc.), a shell prompt is far easier for an on-bench
#    technician to recover with than a kernel panic.
# 3. systemd's `Type=oneshot` swallows `curses` stty changes — running the
#    TUI as a foreground process from a wrapper preserves terminal state.
#
# Lifecycle:
#   1. Source /opt/vstl/config.env so VSTL_API_BASE/KEY are visible to the TUI.
#   2. Prefer wired/USB Ethernet; fall back to interactive Wi-Fi selection.
#   3. switch to a clean operator VT and exec the Python TUI there (curses
#      requires a real tty — systemd's pseudo-pty breaks colour + arrow keys).
#   4. On TUI exit code 0 → reboot by default after the operator presses ENTER.
#      On TUI exit code 2 → drop to a root shell for debugging.
# =============================================================================
set -uo pipefail

CONFIG_FILE="${VSTL_CONFIG:-/opt/vstl/config.env}"
LOG_FILE="${VSTL_LOG:-/var/log/vstl-imaging-bench.log}"
TUI_PATH="/opt/vstl/vstl-imaging-tui.py"
NETWORK_SETUP_PATH="/opt/vstl/vstl_network_setup.py"
TUI_TTY="${VSTL_TUI_TTY:-/dev/tty1}"
TUI_VT="${VSTL_TUI_VT:-${TUI_TTY#/dev/tty}}"
ENTRY_LOCK="${VSTL_ENTRY_LOCK:-/run/vstl-bench-entry.lock}"
ENTRY_PID_FILE="${VSTL_ENTRY_PID_FILE:-/run/vstl-bench-entry.pid}"
ENTRY_LOCK_DIR=""
case "$TUI_VT" in
    ""|*[!0-9]*) TUI_VT=1 ;;
esac

mkdir -p "$(dirname "$LOG_FILE")"

# IMPORTANT: do NOT redirect stdout/stderr through `tee` here. That used to
# read `exec > >(tee -a "$LOG_FILE") 2>&1` and silently broke the Python
# curses TUI further down — `cbreak()` returns ERR when stdin/stdout aren't
# connected to a real tty, and the tee subprocess pipe is not a tty.
# See diagnostic 2026-05-08 (R9125Q20-style symptom — TUI traceback under
# Clonezilla 3.2.2 / Python 3.13 / curses _curses.error: cbreak()).
#
# Instead, the `log()` helper below writes to BOTH the controlling tty and
# the log file, leaving fd 0/1/2 as-is so the TUI can take the tty cleanly.
log() {
    local ts msg
    ts=$(date -Iseconds)
    msg="[$ts] $*"
    printf '%s\n' "$msg" >&2
    printf '%s\n' "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

take_single_instance_lock() {
    # PXE boots can start this script twice: once from Clonezilla's
    # ocs_live_run kernel parameter and once from the baked systemd service.
    # Only one copy may own the operator console; a duplicate waits so
    # Clonezilla does not fall through to a confusing Debian shell prompt.
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$ENTRY_LOCK" || return 0
        if ! flock -n 9; then
            log "Another VSTL bench entry is already running; duplicate launcher will wait"
            return 1
        fi
    else
        ENTRY_LOCK_DIR="${ENTRY_LOCK}.d"
        if ! mkdir "$ENTRY_LOCK_DIR" 2>/dev/null; then
            log "Another VSTL bench entry is already running; duplicate launcher will wait"
            return 1
        fi
    fi
    printf '%s\n' "$$" >"$ENTRY_PID_FILE" 2>/dev/null || true
    return 0
}

wait_for_existing_instance() {
    local owner
    owner="$(cat "$ENTRY_PID_FILE" 2>/dev/null || true)"

    if command -v chvt >/dev/null 2>&1; then
        chvt "$TUI_VT" 2>/dev/null || true
    fi

    if [[ "$owner" =~ ^[0-9]+$ ]]; then
        log "Waiting for existing VSTL entry pid $owner on $TUI_TTY"
        while kill -0 "$owner" 2>/dev/null; do
            sleep 5
        done
        log "Existing VSTL entry pid $owner ended; duplicate launcher exiting"
    else
        log "Existing VSTL entry pid unknown; holding duplicate launcher"
        while true; do sleep 3600; done
    fi
}

cleanup_single_instance_lock() {
    rm -f "$ENTRY_PID_FILE" 2>/dev/null || true
    if [[ -n "$ENTRY_LOCK_DIR" ]]; then
        rm -rf "$ENTRY_LOCK_DIR" 2>/dev/null || true
    fi
}

keep_operator_console_awake() {
    # Long secure erase commands can leave the unit running while the Linux
    # console blanks the panel. Keep the operator VT visible for local/KVM use.
    echo 0 > /sys/module/kernel/parameters/consoleblank 2>/dev/null || true
    if command -v setterm >/dev/null 2>&1; then
        local tty
        for tty in "$TUI_TTY" /dev/tty0 /dev/console; do
            [[ -c "$tty" ]] || continue
            setterm --blank 0 --powerdown 0 --powersave off <"$tty" >"$tty" 2>/dev/null || true
            setterm --blank poke <"$tty" >"$tty" 2>/dev/null || true
        done
    fi
    local power dpms
    for power in /sys/class/backlight/*/bl_power; do
        [[ -w "$power" ]] && echo 0 > "$power" || true
    done
    for dpms in /sys/class/drm/*/dpms; do
        [[ -w "$dpms" ]] && echo On > "$dpms" || true
    done
}

quiet_kernel_console() {
    # Hardware/driver traces must stay available in dmesg/journal, but they
    # must not print over the curses bench UI while restore/erase is running.
    if command -v dmesg >/dev/null 2>&1; then
        dmesg -D >/dev/null 2>&1 || dmesg -n 1 >/dev/null 2>&1 || true
    fi
    printf '1 4 1 7\n' > /proc/sys/kernel/printk 2>/dev/null || true
}

if ! take_single_instance_lock; then
    wait_for_existing_instance
    exit 0
fi
trap cleanup_single_instance_lock EXIT
quiet_kernel_console
keep_operator_console_awake

# ---------- 1. Config ----------------------------------------------------------
if [[ -f "$CONFIG_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
else
    log "WARNING: $CONFIG_FILE missing — TUI will surface this to operator"
fi

# ---------- 2. Launch the Python TUI on a clean operator VT --------------------
if [[ ! -f "$TUI_PATH" ]]; then
    log "FATAL: $TUI_PATH not present — falling back to legacy shell client"
    exec /opt/vstl/vstl-imaging-client.sh
fi

clear_tui_tty() {
    if [[ -c "$TUI_TTY" ]]; then
        # Reset terminal state, clear screen + scrollback, and leave cursor
        # enabled for curses. ESC c is intentionally stronger than `clear`:
        # it removes attributes/cursor state left by firmware, boot scripts,
        # Clonezilla, or a previous aborted TUI.
        printf '\033c\033[?25h\033[H\033[2J\033[3J' >"$TUI_TTY" 2>/dev/null || true
    else
        clear 2>/dev/null || true
    fi
}

apply_console_font() {
    if [[ ! -c "$TUI_TTY" ]]; then
        return 0
    fi
    local font
    for font in \
        "${VSTL_CONSOLE_FONT:-}" \
        /usr/share/consolefonts/Lat15-TerminusBold32x16.psf.gz \
        /usr/share/consolefonts/Lat2-TerminusBold32x16.psf.gz \
        /usr/share/consolefonts/Uni3-TerminusBold32x16.psf.gz \
        /usr/share/consolefonts/Lat15-Terminus32x16.psf.gz \
        /usr/share/consolefonts/Lat2-Terminus32x16.psf.gz
    do
        [[ -n "$font" && -f "$font" ]] || continue
        setfont -C "$TUI_TTY" "$font" >/dev/null 2>&1 \
            || setfont "$font" <"$TUI_TTY" >"$TUI_TTY" 2>/dev/null \
            || true
        return 0
    done
    return 0
}

activate_tui_tty() {
    if [[ ! -c "$TUI_TTY" ]]; then
        return 1
    fi

    stty sane <"$TUI_TTY" >"$TUI_TTY" 2>/dev/null || true
    quiet_kernel_console
    keep_operator_console_awake
    apply_console_font
    clear_tui_tty

    # Keep the operator on the same tty requested by ocs_live_run so PiKVM and
    # physical displays do not fall back to a user@debian prompt. The hard
    # reset above clears boot text before Python/curses starts.
    if command -v chvt >/dev/null 2>&1; then
        chvt "$TUI_VT" 2>/dev/null || true
        sleep 0.2
    fi

    stty sane <"$TUI_TTY" >"$TUI_TTY" 2>/dev/null || true
    quiet_kernel_console
    keep_operator_console_awake
    apply_console_font
    clear_tui_tty
    return 0
}

wait_for_console_quiet() {
    # Clonezilla/live-config can keep printing final setup lines for a few
    # seconds after ocs_live_run/systemd starts us. If curses starts during
    # that window, operators see a half-drawn technician screen first. Switch
    # to the clean operator VT immediately, show only a quiet loading message
    # there, then clear again once boot chatter has settled.
    local delay state
    delay="${VSTL_TUI_START_DELAY_SEC:-5}"

    activate_tui_tty || clear_tui_tty
    if [[ -c "$TUI_TTY" ]]; then
        printf '\033[H\033[2J\033[3J\n\n  Preparing VSTL bench screen...\n' >"$TUI_TTY" 2>/dev/null || true
    fi

    sleep "$delay"
    if command -v systemctl >/dev/null 2>&1; then
        for _ in $(seq 1 12); do
            state="$(systemctl is-system-running 2>/dev/null || true)"
            case "$state" in
                running|degraded|maintenance|stopping|"") break ;;
            esac
            sleep 0.25
        done
    fi
    activate_tui_tty || clear_tui_tty
}

# Ensure terminal is quiet and clean before curses takes over.
wait_for_console_quiet

run_network_setup() {
    if [[ ! -f "$NETWORK_SETUP_PATH" ]]; then
        log "Network helper missing; TUI will launch in offline mode"
        return 1
    fi

    log "Starting wired/USB Ethernet and Wi-Fi network setup"
    if [[ -c "$TUI_TTY" ]]; then
        TERM=linux python3 "$NETWORK_SETUP_PATH" \
            <"$TUI_TTY" >"$TUI_TTY" 2>>"$LOG_FILE"
    else
        TERM="${TERM:-linux}" python3 "$NETWORK_SETUP_PATH" 2>>"$LOG_FILE"
    fi
}

run_network_setup || log "Network unavailable or operator selected offline mode"
activate_tui_tty || clear_tui_tty
TUI_RC=0

open_recovery_shell_or_hold() {
    local reason="$1"
    log "$reason"
    activate_tui_tty || clear_tui_tty
    if [[ -c "$TUI_TTY" ]]; then
        {
            printf '\033c\033[?25h\033[H\033[2J\033[3J'
            printf '\n  VSTL recovery shell\n\n'
            printf '  %s\n\n' "$reason"
            printf '  Type "reboot -f" to restart this unit.\n'
            printf '  Type "journalctl -xb" or "cat %s" to inspect logs.\n\n' "$LOG_FILE"
            printf '  Kernel messages are muted on this screen; use "dmesg" to inspect them.\n\n'
        } >"$TUI_TTY" 2>/dev/null || true
        if [[ -x /bin/bash ]]; then
            exec /bin/bash -li <"$TUI_TTY" >"$TUI_TTY" 2>&1
        fi
        if [[ -x /bin/sh ]]; then
            exec /bin/sh -i <"$TUI_TTY" >"$TUI_TTY" 2>&1
        fi
    fi
    log "No recovery shell could be opened; holding console to avoid live shutdown."
    while true; do sleep 3600; done
}

# Touch controllers are exposed as evdev character devices. Clonezilla/live
# images can preserve restrictive udev modes on some HID-over-I2C hardware,
# even though this ephemeral bench workflow runs as root.
if [[ -d /dev/input ]]; then
    chmod a+r /dev/input/event* 2>/dev/null || true
fi

# Bind the Python TUI to a real tty (Clonezilla's ocs_live_run sometimes
# inherits a non-tty stdin under certain init paths). TERM=linux is the
# correct terminfo entry for the Linux console Clonezilla boots into.
# 2>>"$LOG_FILE" captures any traceback while keeping stdin/stdout as
# the genuine tty so curses.cbreak() succeeds.
if [[ -c "$TUI_TTY" ]]; then
    log "Launching VSTL TUI on $TUI_TTY (TERM=linux)"
    TERM=linux python3 "$TUI_PATH" \
        <"$TUI_TTY" >"$TUI_TTY" 2>>"$LOG_FILE" \
        || TUI_RC=$?
else
    log "WARN: $TUI_TTY missing — running TUI on inherited fds (curses may fail)"
    TERM="${TERM:-linux}" python3 "$TUI_PATH" 2>>"$LOG_FILE" || TUI_RC=$?
fi
log "TUI exited with rc=$TUI_RC"

# ---------- 3. Post-flight ----------------------------------------------------
case "$TUI_RC" in
    0)
        if [[ "${VSTL_AUTO_SHUTDOWN:-1}" == "1" ]]; then
            log "Operator completed workflow; restarting system now."
            echo "VSTL workflow finished. Restarting system now..." >"$TUI_TTY" 2>/dev/null || true
            sync || true
            if command -v systemctl >/dev/null 2>&1; then
                systemctl reboot -i || true
            fi
            reboot || true
            shutdown -r now || true
            sleep 3
            reboot -f || true
        else
            log "VSTL_AUTO_SHUTDOWN=0 - holding console to prevent Clonezilla final-action menu"
            echo "VSTL workflow finished. Restart or power off from the VSTL screen when ready." >"$TUI_TTY" 2>/dev/null || true
            while true; do sleep 3600; done
        fi
        ;;
    2)
        open_recovery_shell_or_hold "TUI requested debug shell (operator pressed Q on completion screen)"
        ;;
    *)
        open_recovery_shell_or_hold "TUI failed unexpectedly — dropping to recovery shell so the operator can investigate"
        ;;
esac

exit "$TUI_RC"
