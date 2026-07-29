"""
vstl_image_restore.py â€” Phase 3 Restore Approved System Image
==============================================================
Looks up the golden copy for the SUT's detected model + part-number,
mounts the NFS golden-copy share, runs Clonezilla's ocs-sr in
``restoredisk`` mode, and returns a structured result so the TUI can
report the outcome to the backend (POST /api/imaging/restore/complete).

Lookup policy per design choice 3c:
    1. Try the backend ``/imaging/lookup`` endpoint with model+part_number.
       If it returns status="found", use it directly.
    2. If status="manual_capture" (no golden copy yet), the caller
       (the TUI) is expected to present the operator with the choice
       to PICK a different golden copy or SKIP/abort. This module
       exposes :func:`list_golden_copies` for that picker.

Result schema
-------------
{
  "ok": True,                       # restore completed successfully
  "image_name":    "...",           # what was restored
  "golden_copy_id": "uuid",         # which golden_copies entry was used
  "started_at":    "ISO-8601",
  "completed_at":  "ISO-8601",
  "duration_sec":  int,
  "device":        "/dev/nvme0n1",
  "verified":      bool,
  "evidence":      "ocs-sr stdout/stderr excerpt",
  "error_message": ""
}
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

# Re-use the helpers from the capture module â€” same mount point + run
from vstl_image_capture import (
    _NFS_MOUNT_POINT,
    _CAPTURE_META_FILE,
    mount_nfs,
    umount_nfs,
    _run,
    _EVIDENCE_CAP,
    _fmt_bytes,
    _parts_from_image_dir,
    validate_partition_layout,
    _update_capture_state_from_line,
    _emit_capture_progress,
)


BENCH_USER_AGENT = "VSTL-Bench/2.0 (Linux; PXE; +https://vstl360.local)"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _api_call(api_base: str, api_key: str, method: str,
               path: str, body: Optional[dict] = None,
               timeout: int = 30) -> tuple[bool, dict, str]:
    """Minimal JSON HTTP client. Returns (ok, parsed_response, raw_error_str)."""
    url = f"{api_base.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "X-API-Key": api_key,
        "User-Agent": BENCH_USER_AGENT,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8") or "{}"), ""
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            detail = {"detail": e.reason}
        return False, detail, f"HTTP {e.code}: {detail}"
    except (urllib.error.URLError, OSError) as e:
        return False, {}, f"network error: {e}"


def _norm_match(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def lookup_golden_copy(api_base: str, api_key: str,
                        model: str, part_number: str = "",
                        cpu_model: str = "") -> dict:
    """Wrap ``GET /imaging/lookup`` so the TUI can decide whether to
    auto-restore (status="found") or prompt the operator (status="manual_capture").
    """
    qs = []
    if model:
        qs.append(f"model={urllib.request.quote(model)}")
    if part_number:
        qs.append(f"part_number={urllib.request.quote(part_number)}")
    if cpu_model:
        qs.append(f"cpu={urllib.request.quote(cpu_model)}")
    path = "/imaging/lookup?" + "&".join(qs) if qs else "/imaging/lookup"
    ok, body, err = _api_call(api_base, api_key, "GET", path)
    if not ok:
        return {"status": "error", "error": err, "raw": body}
    return body


def _dir_size_gb(path: str) -> str:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    if not total:
        return "?"
    return f"{round(total / (1024 ** 3), 2)}"


def _copy_from_metadata(image_dir: str, meta: dict, match_type: str) -> dict:
    image_subdir = os.path.basename(image_dir.rstrip("/"))
    image_name = meta.get("image_name") or image_subdir
    return {
        "id": meta.get("id", ""),
        "model": meta.get("model", ""),
        "part_number": meta.get("part_number") or meta.get("sku", ""),
        "cpu": meta.get("cpu", ""),
        "image_name": image_name,
        "image_subdir": meta.get("image_subdir") or image_subdir,
        "image_path": image_dir,
        "image_size_gb": meta.get("image_size_gb") or _dir_size_gb(image_dir),
        "compression": meta.get("compression", "zstd"),
        "match_type": match_type,
        "os_name": meta.get("os_name", ""),
        "os_version": meta.get("os_version", ""),
        "os_build": meta.get("os_build", ""),
        "os_token": meta.get("os_token", ""),
    }


def find_local_golden_copies(
    nfs_host: str,
    nfs_share: str,
    mount_options: str,
    model: str,
    part_number: str,
    cpu_model: str,
) -> dict:
    """Server Process fallback:
    1) exact SKU/Unit Part Number, 2) exact Model Name + exact CPU.
    """
    ok_m, mount_ev = mount_nfs(nfs_host, nfs_share, mount_options)
    if not ok_m:
        return {"status": "error", "error": mount_ev, "evidence": mount_ev}

    sku_want = _norm_match(part_number)
    model_want = _norm_match(model)
    cpu_want = _norm_match(cpu_model)
    sku_matches: list[dict] = []
    model_cpu_matches: list[dict] = []
    try:
        for name in sorted(os.listdir(_NFS_MOUNT_POINT)):
            if name.endswith("_old") or name in {"postinitscripts"}:
                continue
            image_dir = os.path.join(_NFS_MOUNT_POINT, name)
            if not os.path.isdir(image_dir):
                continue
            meta_path = os.path.join(image_dir, _CAPTURE_META_FILE)
            meta: dict = {}
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f) or {}
                except (OSError, json.JSONDecodeError):
                    meta = {}
            if not meta:
                meta = {"image_name": name, "model": "", "part_number": "", "cpu": ""}

            meta_sku = _norm_match(meta.get("part_number") or meta.get("sku") or "")
            meta_model = _norm_match(meta.get("model") or "")
            meta_cpu = _norm_match(meta.get("cpu") or "")
            if sku_want and meta_sku and meta_sku == sku_want:
                sku_matches.append(_copy_from_metadata(image_dir, meta, "sku"))
            elif model_want and cpu_want and meta_model == model_want and meta_cpu == cpu_want:
                model_cpu_matches.append(_copy_from_metadata(image_dir, meta, "model_cpu"))
    except OSError as e:
        return {"status": "error", "error": f"NFS scan failed: {e}", "evidence": mount_ev}

    def _sort_key(copy: dict) -> tuple[str, str, str]:
        return (
            _norm_match(copy.get("os_name", "")),
            _norm_match(copy.get("os_version", "")),
            _norm_match(copy.get("image_name", "")),
        )

    if sku_matches:
        return {
            "status": "found",
            "copies": sorted(sku_matches, key=_sort_key),
            "golden_copy": sorted(sku_matches, key=_sort_key)[-1],
            "match_type": "sku",
            "evidence": mount_ev,
        }
    if model_cpu_matches:
        return {
            "status": "found",
            "copies": sorted(model_cpu_matches, key=_sort_key),
            "golden_copy": sorted(model_cpu_matches, key=_sort_key)[-1],
            "match_type": "model_cpu",
            "evidence": mount_ev,
        }
    return {"status": "not_found", "error": "Device Backup not found.", "evidence": mount_ev}


def find_local_golden_copy(
    nfs_host: str,
    nfs_share: str,
    mount_options: str,
    model: str,
    part_number: str,
    cpu_model: str,
) -> dict:
    result = find_local_golden_copies(
        nfs_host, nfs_share, mount_options, model, part_number, cpu_model,
    )
    if result.get("status") == "found" and result.get("copies"):
        result["golden_copy"] = result["copies"][-1]
    return result


def list_golden_copies(api_base: str, api_key: str) -> list[dict]:
    """For the operator-picker fallback flow (design 3c). Uses an admin
    endpoint that's actually JWT-protected, so this only works when the
    bench is configured with a JWT-style token. If the call fails the
    TUI will fall back to a "type the image name" entry box.
    """
    ok, body, _err = _api_call(api_base, api_key, "GET",
                                 "/imaging/golden-copies?active_only=true")
    if not ok:
        return []
    return body.get("copies", []) or []


def _ocs_restoredisk(image_subdir: str, device: str, image_dir: str = "",
                      progress_callback: Optional[Callable[[dict], None]] = None,
                      timeout: int = 4 * 3600) -> tuple[bool, str]:
    """Run Clonezilla ``ocs-sr restoredisk`` and stream partclone progress."""
    dev_name = os.path.basename(device)
    image_dir = image_dir or os.path.join(_NFS_MOUNT_POINT, image_subdir)
    cmd = [
        "ocs-sr", "-batch", "--nogui", "-or", _NFS_MOUNT_POINT,
        "-g", "auto", "-e1", "auto", "-e2", "-r",
        "-icds", "-j2", "-p", "true",
        "restoredisk", image_subdir, dev_name,
    ]
    env = dict(os.environ)
    env.setdefault("OCS_ROOT", "/")
    env["OCSROOT"] = _NFS_MOUNT_POINT
    env["ocsroot"] = _NFS_MOUNT_POINT
    started = time.monotonic()
    state = {
        "operation": "restoring",
        "phase": "starting Clonezilla",
        "elapsed_sec": 0,
        "image_subdir": image_subdir,
        "image_dir": image_dir,
        "device": device,
        "parts": _parts_from_image_dir(image_dir),
        "completed_partitions": [],
        "partition_percent": 0.0,
        "overall_percent": 0.0,
        "last_line": "",
    }
    speed_state: dict = {}
    evidence_lines: list[str] = [
        f"$ {' '.join(cmd)}",
        f"OCSROOT={env.get('OCSROOT')}",
    ]
    _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=env,
        )
    except FileNotFoundError:
        return False, "ocs-sr: not found (is Clonezilla Live booted?)"
    except OSError as e:
        return False, f"OSError: {e}"

    buffer = ""
    last_emit = 0.0
    timed_out = False
    while True:
        now = time.monotonic()
        if now - started > timeout:
            timed_out = True
            proc.kill()
            evidence_lines.append(f"ocs-sr timeout after {timeout}s")
            break

        if proc.stdout is None:
            break
        readable, _, _ = select.select([proc.stdout], [], [], 0.5)
        if readable:
            chunk = os.read(proc.stdout.fileno(), 4096).decode("utf-8", "replace")
            if chunk:
                buffer += chunk
                pieces = buffer.replace("\r", "\n").split("\n")
                buffer = pieces.pop() if pieces else ""
                for piece in pieces:
                    piece = piece.strip()
                    if not piece:
                        continue
                    evidence_lines.append(piece)
                    if len(evidence_lines) > 500:
                        evidence_lines = evidence_lines[-500:]
                    _update_capture_state_from_line(piece, state)
            elif proc.poll() is not None:
                break

        if now - last_emit >= 1.0:
            _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)
            last_emit = now
        if proc.poll() is not None:
            break

    if buffer.strip():
        evidence_lines.append(buffer.strip())
        _update_capture_state_from_line(buffer, state)

    rc_value = proc.wait() if not timed_out else 124
    elapsed = int(time.monotonic() - started)
    state["phase"] = "completed" if rc_value == 0 else "failed"
    state["elapsed_sec"] = elapsed
    if rc_value == 0:
        state["partition_percent"] = 100.0
        state["overall_percent"] = 100.0
        state["overall_eta_sec"] = 0
        state["partition_eta_sec"] = 0
        state["partition_eta_text"] = "0:00:00"
    _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)
    ev = (
        "\n".join(evidence_lines)[-_EVIDENCE_CAP:] +
        f"\nrc={rc_value}\nelapsed_sec={elapsed}"
    )
    return rc_value == 0, ev

def _verify_restore(device: str) -> tuple[bool, str]:
    """Run ``partprobe`` + ``lsblk`` after the restore and confirm a fresh
    partition table is visible. This isn't cryptographic verification â€”
    Clonezilla's own partclone phase verifies block hashes â€” but it gives
    the operator a quick visual sanity check on the result.
    """
    _run(["partprobe", device], timeout=10)
    rc, out, err = _run(
        ["lsblk", "-no", "NAME,SIZE,TYPE,FSTYPE,LABEL", device], timeout=10,
    )
    if rc != 0:
        return False, f"lsblk rc={rc} err={err[:200]}"
    has_part = any(line.split() and "part" in line.split() for line in out.splitlines())
    return has_part, out[:2000]


_WINDOWS_RW_MOUNT_POINT = "/mnt/vstl-restored-windows"


def _mount_restored_windows_rw(device: str) -> tuple[bool, str, str]:
    """Mount restored Windows C: read-write so we can seed first-boot actions."""
    layout = validate_partition_layout(device)
    partition = layout.get("os_partition") or ""
    if not partition:
        issues = "; ".join(layout.get("issues") or [])
        return False, "", f"no Windows OS partition found after restore: {issues}"

    os.makedirs(_WINDOWS_RW_MOUNT_POINT, exist_ok=True)
    _run(["umount", _WINDOWS_RW_MOUNT_POINT], timeout=10)
    attempts = [
        ["mount", "-t", "ntfs3", "-o", "rw", partition, _WINDOWS_RW_MOUNT_POINT],
        ["ntfs-3g", "-o", "rw", partition, _WINDOWS_RW_MOUNT_POINT],
        ["mount", "-o", "rw", partition, _WINDOWS_RW_MOUNT_POINT],
    ]
    evidence: list[str] = []
    for cmd in attempts:
        rc, out, err = _run(cmd, timeout=30)
        evidence.append(f"$ {' '.join(cmd)} rc={rc} {out[:120]} {err[:220]}")
        if rc == 0:
            windows_dir = os.path.join(_WINDOWS_RW_MOUNT_POINT, "Windows")
            if os.path.isdir(windows_dir):
                return True, _WINDOWS_RW_MOUNT_POINT, "\n".join(evidence)
            _run(["umount", _WINDOWS_RW_MOUNT_POINT], timeout=10)
            evidence.append("mounted partition did not contain Windows directory")
    return False, "", "\n".join(evidence)


def _win_join(*parts: str) -> str:
    return "\\".join(part.strip("\\/") for part in parts if part)


def _post_restore_firmware_powershell() -> str:
    """Windows-side script seeded after restore.

    Clonezilla restores disk bytes only. Firmware updates are Windows/OEM
    actions, so this script runs once after the restored Windows installation
    boots and asks Windows Update for driver/firmware capsules.
    """
    return r'''$ErrorActionPreference = "Continue"
$root = Join-Path $env:ProgramData "VSTL\PostRestoreFirmware"
$log = Join-Path $root "post-restore-firmware.log"
$done = Join-Path $root "done.marker"
New-Item -ItemType Directory -Force -Path $root | Out-Null
function Log($msg) {
  $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
  Add-Content -Path $log -Value $line
}
if (Test-Path $done) {
  Log "Already completed; exiting."
  exit 0
}
Log "VSTL post-restore firmware/driver trigger started."
try {
  pnputil /scan-devices | Out-String | ForEach-Object { Log $_.Trim() }
} catch {
  Log ("pnputil scan failed: " + $_.Exception.Message)
}
try {
  $session = New-Object -ComObject Microsoft.Update.Session
  $searcher = $session.CreateUpdateSearcher()
  Log "Searching Windows Update for driver and firmware updates."
  $results = $searcher.Search("IsInstalled=0 and Type='Driver'")
  $updates = New-Object -ComObject Microsoft.Update.UpdateColl
  for ($i = 0; $i -lt $results.Updates.Count; $i++) {
    $update = $results.Updates.Item($i)
    $title = [string]$update.Title
    if ($title -match "(?i)firmware|bios|uefi|system firmware|driver|dell|hp|hewlett|lenovo|intel") {
      Log ("Queueing update: " + $title)
      if (-not $update.EulaAccepted) { $update.AcceptEula() | Out-Null }
      [void]$updates.Add($update)
    } else {
      Log ("Skipping non-firmware driver update: " + $title)
    }
  }
  if ($updates.Count -gt 0) {
    $downloader = $session.CreateUpdateDownloader()
    $downloader.Updates = $updates
    $download = $downloader.Download()
    Log ("Download result: " + $download.ResultCode)
    $installer = $session.CreateUpdateInstaller()
    $installer.Updates = $updates
    $install = $installer.Install()
    Log ("Install result: " + $install.ResultCode + "; rebootRequired=" + $install.RebootRequired)
    if ($install.RebootRequired) {
      Log "Reboot required by firmware/driver update; scheduling reboot."
      shutdown.exe /r /t 90 /c "VSTL firmware/driver update completed. Rebooting to apply update."
    }
  } else {
    Log "No matching driver/firmware updates found."
  }
} catch {
  Log ("Windows Update firmware/driver trigger failed: " + $_.Exception.Message)
}
try {
  $startup = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Startup\VSTL-PostRestoreFirmware.cmd"
  if (Test-Path $startup) {
    Remove-Item -Force $startup
  }
} catch {}
New-Item -ItemType File -Force -Path $done | Out-Null
Log "VSTL post-restore firmware/driver trigger finished."
'''


def _post_restore_firmware_cmd(script_win_path: str) -> str:
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{script_win_path}\""
        " >> \"%ProgramData%\\VSTL\\PostRestoreFirmware\\launcher.log\" 2>&1\r\n"
        "exit /b 0\r\n"
    )


def _append_once(path: str, marker: str, line: str) -> bool:
    existing = ""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                existing = f.read()
        if marker in existing:
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\r\n") as f:
            if existing and not existing.endswith(("\n", "\r")):
                f.write("\r\n")
            if not existing:
                f.write("@echo off\r\n")
            f.write(f"REM {marker}\r\n")
            f.write(line.rstrip() + "\r\n")
        return True
    except OSError:
        return False


def install_post_restore_firmware_trigger(device: str) -> dict:
    """Seed Windows to run firmware/driver scan on first boot after restore."""
    if (os.environ.get("VSTL_POST_RESTORE_FIRMWARE_TRIGGER", "1") or "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return {"ok": False, "installed": False, "evidence": "disabled by VSTL_POST_RESTORE_FIRMWARE_TRIGGER"}

    ok, mount_root, mount_ev = _mount_restored_windows_rw(device)
    if not ok:
        return {"ok": False, "installed": False, "evidence": mount_ev}

    evidence = [mount_ev]
    try:
        script_dir = os.path.join(mount_root, "ProgramData", "VSTL", "PostRestoreFirmware")
        os.makedirs(script_dir, exist_ok=True)
        script_path = os.path.join(script_dir, "TriggerFirmwareDriverUpdate.ps1")
        cmd_path = os.path.join(script_dir, "RunPostRestoreFirmwareUpdate.cmd")
        script_win = _win_join("C:", "ProgramData", "VSTL", "PostRestoreFirmware", "TriggerFirmwareDriverUpdate.ps1")

        with open(script_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(_post_restore_firmware_powershell())
        with open(cmd_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(_post_restore_firmware_cmd(script_win))
        evidence.append(f"script={script_path}")

        setupcomplete = os.path.join(mount_root, "Windows", "Setup", "Scripts", "SetupComplete.cmd")
        setup_line = (
            f"call \"{_win_join('C:', 'ProgramData', 'VSTL', 'PostRestoreFirmware', 'RunPostRestoreFirmwareUpdate.cmd')}\""
        )
        setup_added = _append_once(setupcomplete, "VSTL_POST_RESTORE_FIRMWARE_TRIGGER", setup_line)
        evidence.append(f"setupcomplete={'added' if setup_added else 'already-present-or-failed'}")

        startup_dir = os.path.join(
            mount_root,
            "ProgramData", "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
        )
        os.makedirs(startup_dir, exist_ok=True)
        startup_cmd = os.path.join(startup_dir, "VSTL-PostRestoreFirmware.cmd")
        with open(startup_cmd, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(_post_restore_firmware_cmd(script_win))
        evidence.append(f"startup={startup_cmd}")
        return {
            "ok": True,
            "installed": True,
            "method": "SetupComplete.cmd + all-users Startup fallback",
            "evidence": "\n".join(evidence)[-_EVIDENCE_CAP:],
        }
    except OSError as e:
        evidence.append(f"write failed: {e}")
        return {"ok": False, "installed": False, "evidence": "\n".join(evidence)[-_EVIDENCE_CAP:]}
    finally:
        _run(["sync"], timeout=20)
        _run(["umount", mount_root], timeout=20)


def run_restore(
    image_subdir: str,
    device: str,
    nfs_host: str,
    nfs_share: str,
    mount_options: str = "rw,nolock,vers=3",
    golden_copy_id: str = "",
    golden_copy: Optional[dict] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> dict:
    """End-to-end: mount NFS, restore via ``ocs-sr restoredisk``, verify
    partition table, return the structured result. The TUI is responsible
    for finally unmounting NFS via :func:`umount_nfs`.
    """
    started_at = _now_iso()
    started_ts = time.monotonic()
    golden_copy = golden_copy or {}
    image_subdir = (
        golden_copy.get("image_subdir")
        or image_subdir
        or str(golden_copy.get("image_name", "")).removesuffix(".img")
    )
    image_name = golden_copy.get("image_name") or image_subdir
    image_path = golden_copy.get("image_path") or ""
    os_name = golden_copy.get("os_name", "")
    os_version = golden_copy.get("os_version", "")
    os_build = golden_copy.get("os_build", "")
    os_token = golden_copy.get("os_token", "")
    match_type = golden_copy.get("match_type", "")

    ok_m, mount_ev = mount_nfs(nfs_host, nfs_share, mount_options)
    if not ok_m:
        return {
            "ok": False,
            "result": "FAIL",
            "image_name": image_name,
            "image_subdir": image_subdir,
            "golden_copy_id": golden_copy_id,
            "device": device,
            "os_name": os_name,
            "os_version": os_version,
            "os_build": os_build,
            "os_token": os_token,
            "match_type": match_type,
            "started_at": started_at,
            "completed_at": _now_iso(),
            "duration_sec": int(time.monotonic() - started_ts),
            "verified": False,
            "evidence": mount_ev[:_EVIDENCE_CAP],
            "error_message": f"NFS mount failed: {mount_ev}",
        }

    # Sanity check: does the image directory exist on the share?
    image_dir = image_path if image_path.startswith(_NFS_MOUNT_POINT) else os.path.join(_NFS_MOUNT_POINT, image_subdir)
    if not os.path.isdir(image_dir):
        return {
            "ok": False,
            "result": "FAIL",
            "image_name": image_name,
            "image_subdir": image_subdir,
            "golden_copy_id": golden_copy_id,
            "device": device,
            "os_name": os_name,
            "os_version": os_version,
            "os_build": os_build,
            "os_token": os_token,
            "match_type": match_type,
            "started_at": started_at,
            "completed_at": _now_iso(),
            "duration_sec": int(time.monotonic() - started_ts),
            "verified": False,
            "evidence": f"image dir not found on NFS: {image_dir}",
            "error_message": f"Golden copy '{image_subdir}' not present at {image_dir}",
        }

    ok_r, restore_ev = _ocs_restoredisk(
        image_subdir, device, image_dir=image_dir, progress_callback=progress_callback,
    )
    duration = int(time.monotonic() - started_ts)
    if not ok_r:
        return {
            "ok": False,
            "result": "FAIL",
            "image_name": image_name,
            "image_subdir": image_subdir,
            "golden_copy_id": golden_copy_id,
            "device": device,
            "os_name": os_name,
            "os_version": os_version,
            "os_build": os_build,
            "os_token": os_token,
            "match_type": match_type,
            "started_at": started_at,
            "completed_at": _now_iso(),
            "duration_sec": duration,
            "verified": False,
            "evidence": ("\n---\n".join([mount_ev, restore_ev]))[:_EVIDENCE_CAP],
            "error_message": "ocs-sr restoredisk failed",
        }

    verified, verify_ev = _verify_restore(device)
    firmware_trigger = install_post_restore_firmware_trigger(device)
    return {
        "ok": True,
        "result": "SUCCESS",
        "image_name": image_name,
        "image_subdir": image_subdir,
        "golden_copy_id": golden_copy_id,
        "device": device,
        "os_name": os_name,
        "os_version": os_version,
        "os_build": os_build,
        "os_token": os_token,
        "match_type": match_type,
        "started_at": started_at,
        "completed_at": _now_iso(),
        "duration_sec": duration,
        "verified": verified,
        "firmware_update_trigger": firmware_trigger,
        "evidence": (
            "\n---\n".join([
                mount_ev,
                restore_ev,
                f"$ post-restore verify\n{verify_ev}",
                "$ post-restore firmware/driver trigger\n" + firmware_trigger.get("evidence", ""),
            ])
        )[:_EVIDENCE_CAP],
        "error_message": "",
    }
