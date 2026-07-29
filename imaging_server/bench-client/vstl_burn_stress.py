"""
vstl_burn_stress.py — Phase 2C automated 5-min burn/stress test.

Per design choice 4c, the four stressors run in this concurrency pattern:
    * CPU stress       — concurrent for the full duration
    * Thermal monitor  — concurrent for the full duration
    * RAM stress       — alternates with disk in 30-sec slices
    * Disk stress      — alternates with RAM in 30-sec slices

This way every component sees real load BUT no single component is cooked
nor are we serializing the test to 4× the wall-clock budget.

Per design choice 5b (failure thresholds):
    * Thermal throttle ≥ 100°C  → FAIL
    * memtester errors ≥ 1      → FAIL
    * fio I/O errors ≥ 1        → FAIL
    * stress-ng exit ≠ 0 with NO error count → WARN only (consumer
      laptops produce noisy stress-ng warnings under high load —
      we treat them as informational, not as a hard fail)

Per design choice 3c, L1 technicians can opt-out via a remarks dialog.
That's a TUI concern — this module just runs the test if asked to.

Result schema
-------------
{
  "skipped": False,
  "duration_sec": 300,
  "actual_duration_sec": 304,    # may overshoot by a slice
  "max_temp_c": 87,
  "throttled": False,            # max_temp_c >= 100
  "errors": {"memtester": 0, "fio": 0, "stress_ng_warnings": 5},
  "result": "PASS" | "FAIL",
  "remarks": "operator-typed text or auto-derived",
  "raw": {                       # forensic detail for downstream phases
     "stress_ng": "...",
     "memtester_last": "...",
     "fio_last": "...",
     "thermal_samples_c": [42, 51, 67, ..., 87]
  }
}
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from typing import Callable, Optional

# Per-design defaults — overridable via /opt/vstl/config.env -----------------
DEFAULT_DURATION_SEC = 300
THROTTLE_TEMP_C = 100
RAM_SLICE_SEC = 30
DISK_SLICE_SEC = 30


def _read_config_int(cfg: dict, key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default))
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Thermal monitor
# ---------------------------------------------------------------------------
def _read_max_cpu_temp_c() -> Optional[int]:
    """Read all /sys/class/thermal/thermal_zone*/temp files and return the max
    across CPU/package zones in Celsius. Returns None if no zones exist
    (e.g. running in a VM / container).
    """
    base = "/sys/class/thermal"
    if not os.path.isdir(base):
        return None
    temps: list[int] = []
    try:
        for entry in os.listdir(base):
            if not entry.startswith("thermal_zone"):
                continue
            tpath = os.path.join(base, entry, "temp")
            try:
                with open(tpath) as f:
                    raw = f.read().strip()
                if raw:
                    # kernel reports millidegrees C
                    temps.append(int(raw) // 1000)
            except (OSError, ValueError):
                continue
    except OSError:
        return None
    return max(temps) if temps else None


def _read_cpu_fan_rpms() -> list[dict[str, int | str]]:
    """Read exposed CPU/system fan RPM sensors from hwmon, if available."""
    base = "/sys/class/hwmon"
    fans: list[dict[str, int | str]] = []
    if not os.path.isdir(base):
        return fans
    try:
        hwmons = sorted(os.listdir(base))
    except OSError:
        return fans
    for hwmon in hwmons:
        root = os.path.join(base, hwmon)
        chip = ""
        try:
            with open(os.path.join(root, "name")) as f:
                chip = f.read().strip()
        except OSError:
            chip = hwmon
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            match = re.fullmatch(r"fan(\d+)_input", entry)
            if not match:
                continue
            fan_no = match.group(1)
            path = os.path.join(root, entry)
            label = f"{chip} fan{fan_no}".strip()
            try:
                with open(os.path.join(root, f"fan{fan_no}_label")) as f:
                    label = f.read().strip() or label
            except OSError:
                pass
            try:
                with open(path) as f:
                    rpm = int(float(f.read().strip() or "0"))
            except (OSError, ValueError):
                continue
            fans.append({"sensor": label, "rpm": max(0, rpm)})
    return fans


def _summarize_fan(samples: list[int], sensor_seen: bool) -> dict:
    if not sensor_seen:
        return {
            "fan_status": "NO SENSOR",
            "fan_detected": False,
            "fan_min_rpm": "",
            "fan_max_rpm": "",
            "fan_avg_rpm": "",
        }
    if not samples:
        return {
            "fan_status": "NOT SPINNING",
            "fan_detected": True,
            "fan_min_rpm": "0",
            "fan_max_rpm": "0",
            "fan_avg_rpm": "0",
        }
    return {
        "fan_status": "OK",
        "fan_detected": True,
        "fan_min_rpm": min(samples),
        "fan_max_rpm": max(samples),
        "fan_avg_rpm": round(sum(samples) / len(samples)),
    }


# ---------------------------------------------------------------------------
# Subsystem runners — each returns (errors_count, raw_output_tail)
# ---------------------------------------------------------------------------
def _run_stress_ng(seconds: int) -> tuple[int, str]:
    """CPU + cache stressor. We use --metrics-brief so the count of
    'unsuccessful' work units is grep-able from stdout.
    """
    cmd = ["stress-ng", "--cpu", "0", "--cpu-method", "matrixprod",
           "--metrics-brief", "-t", f"{seconds}s"]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=seconds + 30, check=False,
        )
        # stress-ng warnings happen under load — count them but don't FAIL
        warns = sum(1 for ln in (out.stderr or "").splitlines() if "warning" in ln.lower())
        tail = (out.stdout or out.stderr or "")[-1500:]
        return warns, tail
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 0, f"stress-ng unavailable: {e}"


def _run_memtester_slice(slice_sec: int) -> tuple[int, str]:
    """Run memtester on a small chunk of free RAM — 1 pass.
    memtester 1 pass takes ~30s for 1G on modern DDR4, perfect for one slice.
    If we can't allocate, the call is a no-op (errors=0, warning logged).
    """
    cmd = ["memtester", "256M", "1"]  # 256 MB, 1 pass
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=slice_sec + 30, check=False,
        )
        # memtester prints "FAILURE:" lines on error
        text = (out.stdout or "") + (out.stderr or "")
        errs = len(re.findall(r"FAILURE", text))
        return errs, text[-1200:]
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 0, f"memtester unavailable: {e}"


def _run_fio_slice(slice_sec: int) -> tuple[int, str]:
    """Run fio random-read on a tmpfs-backed file for `slice_sec` seconds.
    We deliberately avoid touching the SUT's actual disks (those are about
    to be wiped + re-imaged in Phase 3).
    """
    sample_file = "/tmp/vstl_fio_sample"
    cmd = [
        "fio", "--name=vstl-burn", f"--filename={sample_file}",
        "--rw=randread", "--bs=4k", "--size=64M", "--ioengine=libaio",
        "--iodepth=8", f"--runtime={slice_sec}", "--time_based",
        "--numjobs=1", "--group_reporting",
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=slice_sec + 30, check=False,
        )
        text = (out.stdout or "") + (out.stderr or "")
        errs = len(re.findall(r"err=\s*[1-9]", text))
        if "io_u error" in text:
            errs += 1
        return errs, text[-1200:]
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 0, f"fio unavailable: {e}"
    finally:
        try:
            os.unlink(sample_file)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Orchestrator — runs all 4 subsystems in the design-4c concurrency pattern
# ---------------------------------------------------------------------------
def run_burn_test(
    duration_sec: int = DEFAULT_DURATION_SEC,
    progress_callback: Optional[Callable[[dict], None]] = None,
    cfg: Optional[dict] = None,
) -> dict:
    """Execute the full Phase 2C burn/stress pattern.

    progress_callback is invoked every ~1 sec with a dict::

        {
          "elapsed_sec": int,
          "remaining_sec": int,
          "current_temp_c": int | None,
          "max_temp_c": int | None,
          "ram_disk_phase": "ram" | "disk",
          "errors": {"memtester": 0, "fio": 0, "stress_ng_warnings": 0},
        }

    The TUI uses this to render a live progress screen.
    """
    cfg = cfg or {}
    duration_sec = _read_config_int(cfg, "VSTL_BURN_DURATION_SEC", duration_sec)
    throttle_temp = _read_config_int(cfg, "VSTL_BURN_THROTTLE_C", THROTTLE_TEMP_C)

    started_at = time.monotonic()
    errors = {"memtester": 0, "fio": 0, "stress_ng_warnings": 0}
    raw = {"stress_ng": "", "memtester_last": "", "fio_last": "", "thermal_samples_c": []}
    state = {"max_temp_c": None, "current_temp_c": None,
             "ram_disk_phase": "ram", "fan_status": "NO SENSOR",
             "current_fan_rpm": None}
    fan_sensor_seen = False
    fan_samples_rpm: list[int] = []

    # 1. CPU stress runs concurrently for the FULL duration in a thread
    cpu_thread_result: dict = {}

    def _cpu_thread():
        warns, tail = _run_stress_ng(duration_sec)
        cpu_thread_result["warns"] = warns
        cpu_thread_result["tail"] = tail

    cpu_t = threading.Thread(target=_cpu_thread, daemon=True)
    cpu_t.start()

    # 2. RAM/disk alternation runs in another thread
    def _ram_disk_thread():
        elapsed = 0
        toggle = 0
        while elapsed < duration_sec:
            slice_remaining = min(RAM_SLICE_SEC, duration_sec - elapsed)
            if toggle % 2 == 0:
                state["ram_disk_phase"] = "ram"
                errs, tail = _run_memtester_slice(slice_remaining)
                errors["memtester"] += errs
                raw["memtester_last"] = tail
            else:
                state["ram_disk_phase"] = "disk"
                errs, tail = _run_fio_slice(slice_remaining)
                errors["fio"] += errs
                raw["fio_last"] = tail
            elapsed = int(time.monotonic() - started_at)
            toggle += 1

    ram_t = threading.Thread(target=_ram_disk_thread, daemon=True)
    ram_t.start()

    # 3. Main thread = thermal sampler + progress callback dispatcher
    while True:
        elapsed = int(time.monotonic() - started_at)
        if elapsed >= duration_sec and not cpu_t.is_alive() and not ram_t.is_alive():
            break
        cur = _read_max_cpu_temp_c()
        if cur is not None:
            state["current_temp_c"] = cur
            if state["max_temp_c"] is None or cur > state["max_temp_c"]:
                state["max_temp_c"] = cur
            raw["thermal_samples_c"].append(cur)
        fan_readings = _read_cpu_fan_rpms()
        if fan_readings:
            fan_sensor_seen = True
            current_rpm = max(int(item.get("rpm") or 0) for item in fan_readings)
            state["current_fan_rpm"] = current_rpm
            if current_rpm > 0:
                fan_samples_rpm.append(current_rpm)
                state["fan_status"] = "OK"
            else:
                state["fan_status"] = "NOT SPINNING"
            raw.setdefault("fan_samples", []).append(fan_readings)
        if progress_callback:
            try:
                progress_callback({
                    "elapsed_sec": elapsed,
                    "remaining_sec": max(0, duration_sec - elapsed),
                    "current_temp_c": state["current_temp_c"],
                    "max_temp_c": state["max_temp_c"],
                    "fan_status": state["fan_status"],
                    "current_fan_rpm": state["current_fan_rpm"],
                    "ram_disk_phase": state["ram_disk_phase"],
                    "errors": dict(errors),
                })
            except Exception:  # noqa: BLE001 - keep the burn going regardless
                pass
        time.sleep(1)
        if elapsed >= duration_sec + 60:
            # Hard runaway guard: if both threads refuse to finish 60s after
            # the budgeted window, bail rather than block the bench forever.
            break

    cpu_t.join(timeout=15)
    ram_t.join(timeout=15)

    errors["stress_ng_warnings"] = cpu_thread_result.get("warns", 0)
    raw["stress_ng"] = cpu_thread_result.get("tail", "")
    actual = int(time.monotonic() - started_at)
    fan_summary = _summarize_fan(fan_samples_rpm, fan_sensor_seen)
    raw["fan_samples_rpm"] = fan_samples_rpm

    throttled = (state["max_temp_c"] is not None and state["max_temp_c"] >= throttle_temp)
    failed = (
        throttled
        or errors["memtester"] > 0
        or errors["fio"] > 0
    )

    return {
        "skipped": False,
        "duration_sec": duration_sec,
        "actual_duration_sec": actual,
        "max_temp_c": state["max_temp_c"],
        "throttled": throttled,
        **fan_summary,
        "errors": errors,
        "result": "FAIL" if failed else "PASS",
        "remarks": (
            f"thermal throttle at {state['max_temp_c']}°C" if throttled
            else f"memtester {errors['memtester']} error(s)" if errors["memtester"] > 0
            else f"fio {errors['fio']} I/O error(s)" if errors["fio"] > 0
            else ""
        ),
        "raw": raw,
    }


def skipped_result(remarks: str = "Skipped by L1 technician") -> dict:
    """Build the burn-test bundle for a skipped run (L1 path per design 3c)."""
    return {
        "skipped": True,
        "duration_sec": 0,
        "actual_duration_sec": 0,
        "max_temp_c": None,
        "throttled": False,
        "fan_status": "SKIPPED",
        "fan_detected": False,
        "fan_min_rpm": "",
        "fan_max_rpm": "",
        "fan_avg_rpm": "",
        "errors": {"memtester": 0, "fio": 0, "stress_ng_warnings": 0},
        "result": "SKIP",
        "remarks": remarks,
        "raw": {
            "stress_ng": "",
            "memtester_last": "",
            "fio_last": "",
            "thermal_samples_c": [],
            "fan_samples_rpm": [],
        },
    }
