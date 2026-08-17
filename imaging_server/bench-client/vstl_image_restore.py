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
import re
import select
import signal
import shlex
import shutil
import stat
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
RESTORE_CLIENT_BUILD = "restore-track-v19"
RESTORE_NFS_MOUNT_OPTIONS = (
    "rw,nolock,vers=3,proto=tcp,hard,timeo=600,retrans=5,"
    "rsize=1048576,wsize=1048576"
)


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


def _dir_mtime_iso(path: str) -> str:
    try:
        return datetime.fromtimestamp(
            os.path.getmtime(path), timezone.utc
        ).isoformat()
    except OSError:
        return ""


def _timestamp_value(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _copy_recency_key(copy: dict) -> tuple[float, str]:
    metadata_ts = max(
        _timestamp_value(copy.get("captured_at")),
        _timestamp_value(copy.get("updated_at")),
        _timestamp_value(copy.get("created_at")),
    )
    ts = metadata_ts or _timestamp_value(copy.get("image_mtime"))
    return (ts, _norm_match(copy.get("image_subdir") or copy.get("image_name") or ""))


def _copy_restore_choice_key(copy: dict) -> tuple[str, str]:
    os_key = _norm_match(
        copy.get("os_token")
        or " ".join(
            str(copy.get(key) or "")
            for key in ("os_name", "os_version", "os_build")
        )
    )
    if not os_key:
        os_key = _norm_match(copy.get("image_name", ""))
    return (_norm_match(copy.get("match_type", "")), os_key)


def latest_restore_copies(copies: list[dict]) -> list[dict]:
    """Collapse duplicate restore choices to the newest captured backup."""
    latest: dict[tuple[str, str], dict] = {}
    for copy in copies:
        key = _copy_restore_choice_key(copy)
        current = latest.get(key)
        if current is None or _copy_recency_key(copy) > _copy_recency_key(current):
            latest[key] = copy
    return sorted(
        latest.values(),
        key=lambda copy: (
            _norm_match(copy.get("os_name", "")),
            _norm_match(copy.get("os_version", "")),
            _norm_match(copy.get("os_build", "")),
            _copy_recency_key(copy),
        ),
    )


def newest_restore_copy(copies: list[dict]) -> dict:
    return max(copies, key=_copy_recency_key) if copies else {}


_RENAMED_BACKUP_SUFFIX_RE = re.compile(
    r"(?:\s+\([2-9]\d*\)|[_\-\s]+[2-9]\d*|(?:\s+-\s+|[_\-\s]+)copy)$",
    re.IGNORECASE,
)


def is_original_restore_backup_name(name: str) -> bool:
    """Return False for rotated or manually duplicated image directories."""
    text = str(name or "").strip()
    if not text or text.startswith(".") or text in {"postinitscripts"}:
        return False
    if text.endswith("_old"):
        return False
    return not _RENAMED_BACKUP_SUFFIX_RE.search(text)


def _copy_from_metadata(image_dir: str, meta: dict, match_type: str) -> dict:
    image_subdir = os.path.basename(image_dir.rstrip("/"))
    image_name = meta.get("image_name") or image_subdir
    image_mtime = _dir_mtime_iso(image_dir)
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
        "captured_at": meta.get("captured_at") or meta.get("created_at", ""),
        "created_at": meta.get("created_at", ""),
        "updated_at": meta.get("updated_at", ""),
        "image_mtime": image_mtime,
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

    if sku_matches:
        copies = latest_restore_copies(sku_matches)
        return {
            "status": "found",
            "copies": copies,
            "golden_copy": newest_restore_copy(copies),
            "match_type": "sku",
            "evidence": mount_ev,
        }
    if model_cpu_matches:
        copies = latest_restore_copies(model_cpu_matches)
        return {
            "status": "found",
            "copies": copies,
            "golden_copy": newest_restore_copy(copies),
            "match_type": "model_cpu",
            "evidence": mount_ev,
        }
    return {"status": "not_found", "error": "Device Backup not found.", "evidence": mount_ev}


def list_local_original_golden_copies(
    nfs_host: str,
    nfs_share: str,
    mount_options: str,
) -> dict:
    """List operator-pickable original image backups from the NFS share."""
    ok_m, mount_ev = mount_nfs(nfs_host, nfs_share, mount_options)
    if not ok_m:
        return {"status": "error", "error": mount_ev, "evidence": mount_ev, "copies": []}

    copies: list[dict] = []
    try:
        for name in sorted(os.listdir(_NFS_MOUNT_POINT)):
            if not is_original_restore_backup_name(name):
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
                meta = {"image_name": name, "image_subdir": name}
            copy = _copy_from_metadata(image_dir, meta, "manual")
            copy["manual_original"] = True
            copies.append(copy)
    except OSError as e:
        return {
            "status": "error",
            "error": f"NFS scan failed: {e}",
            "evidence": mount_ev,
            "copies": [],
        }

    copies = sorted(copies, key=_copy_recency_key, reverse=True)
    if not copies:
        return {
            "status": "not_found",
            "error": "No original backups found.",
            "evidence": mount_ev,
            "copies": [],
        }
    return {"status": "found", "copies": copies, "evidence": mount_ev}


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
        result["golden_copy"] = newest_restore_copy(result["copies"])
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


def _should_continue_after_partclone(line: str, state: dict) -> bool:
    """Return True when Clonezilla needs Enter after a real partclone pass."""
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line or "").strip().lower()
    if "program terminated" not in clean:
        return False
    current = state.get("current_partition") or ""
    parts = state.get("parts") or []
    if not current or (parts and current not in parts):
        return False
    if current not in (state.get("completed_partitions") or []):
        return False
    return (state.get("last_line") or "").startswith(f"Finished {current};")


def _restore_failure_should_retry_without_precreate(evidence: str) -> bool:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", evidence or "").lower()
    broken_markers = (
        "image of this partition is broken",
        "partition image is broken",
        "image is broken",
        "is broken:",
        "crc error",
        "crc check",
        "partclone fail",
    )
    return any(marker in text for marker in broken_markers)


def _restore_failure_allows_crc_salvage(evidence: str) -> bool:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", evidence or "").lower()
    markers = (
        "crc error",
        "crc check",
        "image of this partition is broken",
        "partition image is broken",
        "image is broken",
        "partclone fail",
    )
    return any(marker in text for marker in markers)


def _restore_failure_allows_direct_read_retry(evidence: str) -> bool:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", evidence or "").lower()
    markers = (
        "brokenpipeerror",
        "broken pipe",
        "read error:no such file or directory",
        "read error: no such file or directory",
        "source read error",
        "source image too short",
        "cat:",
        "no such file or directory",
        "stale file handle",
        "input/output error",
    )
    return any(marker in text for marker in markers)


def _terminate_restore_process(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        time.sleep(0.5)
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        return
    except (AttributeError, ProcessLookupError, OSError):
        pass
    try:
        proc.terminate()
        time.sleep(0.5)
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass


def _ocs_restoredisk_once(
    image_subdir: str,
    device: str,
    image_dir: str = "",
    progress_callback: Optional[Callable[[dict], None]] = None,
    timeout: int = 4 * 3600,
    allow_precreate: bool = True,
    ignore_crc: bool = False,
) -> tuple[bool, str, bool]:
    """Run one Clonezilla ``ocs-sr restoredisk`` attempt and stream progress."""
    dev_name = os.path.basename(device)
    image_dir = image_dir or os.path.join(_NFS_MOUNT_POINT, image_subdir)
    if allow_precreate and (os.environ.get("VSTL_RESTORE_PRECREATE_LAYOUT", "1").strip().lower() not in {"0", "false", "no", "off"}):
        prep_ok, precreated_layout, prep_ev = _prepare_target_windows_gpt_from_image(
            image_dir, device, progress_callback
        )
    else:
        prep_ok, precreated_layout, prep_ev = True, False, "precreate skipped: using Clonezilla partition table"
    if not prep_ok:
        return False, prep_ev, precreated_layout
    partition_mode = "-k" if precreated_layout else "-k1"
    cmd = [
        "ocs-sr", "-batch", "--nogui", "-or", _NFS_MOUNT_POINT,
        "-g", "auto", "-e1", "auto", "-e2", partition_mode, "-r",
        "-icds", "-j2", "-p", "true",
        "restoredisk", image_subdir, dev_name,
    ]
    if ignore_crc:
        cmd.insert(cmd.index("restoredisk"), "-icrc")
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
        prep_ev,
        f"$ {' '.join(cmd)}",
        f"OCSROOT={env.get('OCSROOT')}",
        f"ignore_crc={ignore_crc}",
    ]
    _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        return False, "ocs-sr: not found (is Clonezilla Live booted?)", precreated_layout
    except OSError as e:
        return False, f"OSError: {e}", precreated_layout

    buffer = ""
    last_emit = 0.0
    timed_out = False
    early_retry = False
    while True:
        now = time.monotonic()
        if now - started > timeout:
            timed_out = True
            _terminate_restore_process(proc)
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
                    if _restore_failure_should_retry_without_precreate(piece):
                        early_retry = True
                        if precreated_layout:
                            state["phase"] = "retrying without precreated layout"
                            state["last_line"] = (
                                "Clonezilla reported a broken partition image; retrying with image partition table"
                            )
                        else:
                            state["phase"] = "switching to direct partition restore"
                            state["last_line"] = (
                                "Clonezilla reported a broken partition image again; using direct partition restore"
                            )
                        evidence_lines.append(
                            "detected broken partition marker during Clonezilla restore; "
                            "aborting this attempt for fallback handling"
                        )
                        _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)
                        _terminate_restore_process(proc)
                        break
                    if _should_continue_after_partclone(piece, state) and proc.stdin:
                        try:
                            proc.stdin.write(b"\n")
                            proc.stdin.flush()
                            evidence_lines.append(
                                f"sent continue after {state.get('current_partition')}"
                            )
                        except (BrokenPipeError, OSError):
                            pass
                if early_retry:
                    break
            elif proc.poll() is not None:
                break

        if early_retry:
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
    if early_retry:
        rc_value = 125
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
    return rc_value == 0, ev, precreated_layout


def _ocs_restoredisk(image_subdir: str, device: str, image_dir: str = "",
                      progress_callback: Optional[Callable[[dict], None]] = None,
                      timeout: int = 4 * 3600,
                      nfs_host: str = "",
                      nfs_share: str = "",
                      mount_options: str = "rw,nolock,vers=3") -> tuple[bool, str]:
    """Run Clonezilla ``ocs-sr restoredisk`` with one safe layout retry.

    Some Clonezilla/partclone runs report a valid split image as "broken" when
    we precreate a target-sized GPT first. If that happens, retry once with
    Clonezilla recreating the partition table from the image, then the normal
    post-restore expansion step will still grow the Windows layout as needed.
    """
    ok, evidence, precreated_layout = _ocs_restoredisk_once(
        image_subdir,
        device,
        image_dir=image_dir,
        progress_callback=progress_callback,
        timeout=timeout,
        allow_precreate=True,
        ignore_crc=False,
    )
    if ok or not precreated_layout or not _restore_failure_should_retry_without_precreate(evidence):
        return ok, evidence

    _emit_restore_stage(
        progress_callback,
        "Clonezilla reported a broken partition image; retrying with image partition table",
    )
    retry_prep: list[str] = ["--- retry without precreated layout ---"]
    _zap_target_disk_for_restore(device, retry_prep)

    retry_ok, retry_ev, _retry_precreated = _ocs_restoredisk_once(
        image_subdir,
        device,
        image_dir=image_dir,
        progress_callback=progress_callback,
        timeout=timeout,
        allow_precreate=False,
        ignore_crc=False,
    )
    combined = "\n".join([evidence, *retry_prep, retry_ev])[-_EVIDENCE_CAP:]
    if retry_ok or not _restore_failure_should_retry_without_precreate(retry_ev):
        return retry_ok, combined

    _emit_restore_stage(
        progress_callback,
        "Clonezilla still reported broken/CRC image; retrying without Partclone CRC check",
    )
    crc_prep: list[str] = ["--- retry without Partclone CRC check ---"]
    _zap_target_disk_for_restore(device, crc_prep)
    crc_ok, crc_ev, _crc_precreated = _ocs_restoredisk_once(
        image_subdir,
        device,
        image_dir=image_dir,
        progress_callback=progress_callback,
        timeout=timeout,
        allow_precreate=False,
        ignore_crc=True,
    )
    combined = "\n".join([combined, *crc_prep, crc_ev])[-_EVIDENCE_CAP:]
    if crc_ok or not _restore_failure_allows_crc_salvage(crc_ev):
        return crc_ok, combined

    _emit_restore_stage(
        progress_callback,
        "Clonezilla retry reported a broken partition image; using direct partition restore",
    )
    direct_ok, direct_ev = _direct_partclone_restore(
        image_dir,
        device,
        progress_callback=progress_callback,
        timeout=timeout,
        nfs_host=nfs_host,
        nfs_share=nfs_share,
        mount_options=mount_options,
    )
    return direct_ok, "\n".join([combined, "--- direct partclone fallback ---", direct_ev])[-_EVIDENCE_CAP:]


def _restore_failure_summary(evidence: str) -> str:
    wanted = (
        "error", "failed", "fail", "cannot", "unable", "not found",
        "no space", "smaller", "target", "source", "broken", "aborted",
    )
    lines: list[str] = []
    for raw in (evidence or "").splitlines():
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw).strip()
        if not line:
            continue
        lower = line.lower()
        if any(token in lower for token in wanted):
            if line not in lines:
                lines.append(line)
    if not lines:
        for raw in reversed((evidence or "").splitlines()):
            line = raw.strip()
            if line and not line.startswith("$ "):
                lines.append(line)
            if len(lines) >= 2:
                break
        lines.reverse()
    return " | ".join(lines[-2:])[:180]


def _emit_restore_stage(progress_callback: Optional[Callable[[dict], None]],
                        message: str) -> None:
    if not progress_callback:
        return
    progress_callback({
        "phase": message,
        "last_line": message,
        "partition_eta_sec": None,
        "partition_eta_text": "--",
        "network_rate": "--",
    })


_MAX_ALLOWED_TRAILING_FREE_BYTES = 1024 * 1024 * 1024
_RESTORE_LAYOUT_ALIGN_SECTORS = 2048
_MAX_RECOVERY_MOVE_BYTES = 4 * 1024 * 1024 * 1024
_GPT_EFI_TYPE = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
_GPT_MSR_TYPE = "E3C9E316-0B5C-4DB8-817D-F92DF00215AE"
_GPT_WINDOWS_TYPE = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
_GPT_RECOVERY_TYPE = "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC"


def _logical_sector_size(device: str) -> int:
    rc, out, _err = _run(["blockdev", "--getss", device], timeout=10)
    if rc == 0:
        try:
            value = int(out.strip())
            if value > 0:
                return value
        except ValueError:
            pass
    return 512


def _trailing_free_bytes(device: str) -> tuple[int | None, str]:
    """Return usable free bytes after the last partition, if sfdisk can tell."""
    rc, out, err = _run(["sfdisk", "-J", device], timeout=20)
    if rc != 0:
        return None, f"sfdisk free-space check unavailable: rc={rc} {err[:200]}"
    try:
        data = json.loads(out)
        table = data.get("partitiontable") or {}
        partitions = table.get("partitions") or []
        last_lba = int(table.get("lastlba") or 0)
        highest_end = max(
            int(part.get("start") or 0) + int(part.get("size") or 0) - 1
            for part in partitions
            if int(part.get("size") or 0) > 0
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None, f"sfdisk free-space check parse failed: {out[:240]}"
    free_sectors = max(0, last_lba - highest_end)
    free_bytes = free_sectors * _logical_sector_size(device)
    return free_bytes, f"trailing_free_bytes={free_bytes}"


def _part_number(path: str) -> int | None:
    base = os.path.basename(path or "")
    m = re.search(r"(?:p)?([0-9]+)$", base)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _part_path(device: str, number: int) -> str:
    base = os.path.basename(device)
    suffix = f"p{number}" if re.search(r"[0-9]$", base) else str(number)
    return f"{device}{suffix}"


def _read_sfdisk_table(device: str) -> tuple[dict | None, list[dict], str]:
    rc, out, err = _run(["sfdisk", "-J", device], timeout=20)
    if rc != 0:
        return None, [], f"sfdisk unavailable: rc={rc} {err[:200]}"
    try:
        table = (json.loads(out).get("partitiontable") or {})
    except (json.JSONDecodeError, TypeError) as exc:
        return None, [], f"sfdisk parse failed: {exc}"
    return table, table.get("partitions") or [], "sfdisk ok"


def _entry_for_path(parts: list[dict], path: str) -> dict:
    number = _part_number(path)
    for part in parts:
        if part.get("node") == path:
            return part
        if number is not None and _part_number(part.get("node", "")) == number:
            return part
    return {}


def _part_end(part: dict) -> int:
    return int(part.get("start") or 0) + int(part.get("size") or 0) - 1


def _align_down(value: int, alignment: int = _RESTORE_LAYOUT_ALIGN_SECTORS) -> int:
    return max(alignment, (value // alignment) * alignment)


def _sgdisk_attribute_args(number: int, attrs: str) -> list[str]:
    bits: set[int] = set()
    if re.search(r"\bRequiredPartition\b", attrs or "", re.IGNORECASE):
        bits.add(0)
    for match in re.finditer(r"\bGUID:(\d+)\b", attrs or "", re.IGNORECASE):
        try:
            bits.add(int(match.group(1)))
        except ValueError:
            continue
    return [f"--attributes={number}:set:{bit}" for bit in sorted(bits)]


def _sgdisk_metadata_args(number: int, part: dict, preserve_guids: bool = True) -> list[str]:
    args: list[str] = []
    if part.get("type"):
        args.append(f"--typecode={number}:{part['type']}")
    if part.get("name") is not None:
        args.append(f"--change-name={number}:{part.get('name') or ''}")
    if preserve_guids and part.get("uuid"):
        args.append(f"--partition-guid={number}:{part['uuid']}")
    args.extend(_sgdisk_attribute_args(number, part.get("attrs") or ""))
    return args


def _find_image_sfdisk_table(image_dir: str) -> str:
    if not image_dir or not os.path.isdir(image_dir):
        return ""
    disk_file = os.path.join(image_dir, "disk")
    if os.path.isfile(disk_file):
        try:
            with open(disk_file, "r", encoding="utf-8", errors="ignore") as f:
                disk_name = (f.read().split() or [""])[0]
            if disk_name:
                candidate = os.path.join(image_dir, f"{os.path.basename(disk_name)}-pt.sf")
                if os.path.isfile(candidate):
                    return candidate
        except OSError:
            pass
    try:
        candidates = sorted(
            os.path.join(image_dir, name)
            for name in os.listdir(image_dir)
            if name.endswith("-pt.sf")
        )
    except OSError:
        return ""
    return candidates[0] if candidates else ""


def _parse_image_sfdisk_table(path: str) -> tuple[dict, list[dict], str]:
    table: dict = {}
    parts: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError as exc:
        return {}, [], f"cannot read image partition table {path}: {exc}"

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "start=" in line and "size=" in line and ":" in line:
            node, fields = line.split(":", 1)
            node = node.strip()
            number = _part_number(node)
            if number is None:
                continue
            part: dict = {"node": node.strip(), "number": number}
            for match in re.finditer(r"([A-Za-z0-9_-]+)=(?:\"([^\"]*)\"|([^,]+))", fields):
                key = match.group(1).strip()
                value = (match.group(2) if match.group(2) is not None else match.group(3)).strip()
                if key in {"start", "size"}:
                    try:
                        part[key] = int(value)
                    except ValueError:
                        part[key] = 0
                else:
                    part[key] = value
            if int(part.get("start") or 0) > 0 and int(part.get("size") or 0) > 0:
                parts.append(part)
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            table[key.strip().lower()] = value.strip()

    if not parts:
        return table, [], f"no partitions found in image table {path}"
    return table, sorted(parts, key=lambda item: int(item.get("number") or 0)), "image sfdisk table ok"


def _gpt_type(part: dict) -> str:
    return (part.get("type") or "").strip().upper()


def _pick_windows_gpt_parts(parts: list[dict]) -> tuple[dict, list[dict], dict, str]:
    efi_parts = [part for part in parts if _gpt_type(part) == _GPT_EFI_TYPE]
    msr_parts = [part for part in parts if _gpt_type(part) == _GPT_MSR_TYPE]
    recovery_parts = [part for part in parts if _gpt_type(part) == _GPT_RECOVERY_TYPE]
    windows_parts = [part for part in parts if _gpt_type(part) == _GPT_WINDOWS_TYPE]
    if len(efi_parts) != 1:
        return {}, [], {}, "precreate skipped: expected exactly one EFI partition"
    if len(msr_parts) > 1:
        return {}, [], {}, "precreate skipped: expected no more than one MSR partition"
    if len(recovery_parts) != 1:
        return {}, [], {}, "precreate skipped: expected exactly one recovery partition"
    if len(windows_parts) != 1:
        return {}, [], {}, "precreate skipped: expected exactly one Windows data partition"
    recognized = set(id(part) for part in efi_parts + msr_parts + recovery_parts + windows_parts)
    extras = [part for part in parts if id(part) not in recognized]
    if extras:
        return {}, [], {}, "precreate skipped: image has unsupported extra partitions"
    os_part = windows_parts[0]
    rec_part = recovery_parts[0]
    if int(rec_part.get("start") or 0) <= int(os_part.get("start") or 0):
        return {}, [], {}, "precreate skipped: recovery partition is not after Windows"
    return os_part, efi_parts + msr_parts, rec_part, "windows gpt layout ok"


def _target_disk_size_bytes(device: str) -> tuple[int | None, str]:
    rc, out, err = _run(["blockdev", "--getsz", device], timeout=10)
    if rc != 0:
        return None, f"blockdev --getsz failed rc={rc} {err[:200]}"
    try:
        sectors_512 = int(out.strip())
    except ValueError:
        return None, f"blockdev --getsz parse failed: {out[:120]}"
    if sectors_512 <= 0:
        return None, f"blockdev --getsz returned unusable size: {sectors_512}"
    return sectors_512 * 512, f"target_sectors_512={sectors_512}"


def _ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    return -(-numerator // denominator)


def _source_sector_to_target_sector(
    source_sector: int,
    source_sector_size: int,
    target_sector_size: int,
) -> int:
    return _ceil_div(source_sector * source_sector_size, target_sector_size)


def _source_sector_count_to_target_count(
    source_sector_count: int,
    source_sector_size: int,
    target_sector_size: int,
) -> int:
    return _ceil_div(source_sector_count * source_sector_size, target_sector_size)


def _append_restore_cmd_evidence(
    evidence: list[str],
    cmd: list[str],
    timeout: int = 60,
    out_cap: int = 300,
    err_cap: int = 500,
) -> int:
    rc, out, err = _run(cmd, timeout=timeout)
    evidence.append(f"$ {' '.join(cmd)} rc={rc} {out[:out_cap]} {err[:err_cap]}")
    return rc


def _target_partition_paths(device: str) -> list[str]:
    rc, out, _err = _run(["lsblk", "-ln", "-o", "PATH", device], timeout=20)
    if rc != 0:
        return []
    paths = []
    for raw in out.splitlines():
        path = raw.strip()
        if path and path != device:
            paths.append(path)
    return sorted(set(paths), key=len, reverse=True)


def _quiesce_target_disk(device: str, evidence: list[str]) -> None:
    for part in _target_partition_paths(device):
        _append_restore_cmd_evidence(evidence, ["swapoff", part], timeout=20)
        _append_restore_cmd_evidence(evidence, ["umount", "-fl", part], timeout=20)
    _append_restore_cmd_evidence(evidence, ["sync"], timeout=30)
    _append_restore_cmd_evidence(evidence, ["blockdev", "--flushbufs", device], timeout=30)
    _append_restore_cmd_evidence(evidence, ["partprobe", device], timeout=20)
    _append_restore_cmd_evidence(evidence, ["udevadm", "settle"], timeout=20)


def _zero_target_disk_edges(device: str, evidence: list[str]) -> None:
    _append_restore_cmd_evidence(
        evidence,
        ["dd", "if=/dev/zero", f"of={device}", "bs=1M", "count=32", "conv=fsync"],
        timeout=120,
    )
    rc, out, err = _run(["blockdev", "--getsz", device], timeout=20)
    evidence.append(f"$ blockdev --getsz {device} rc={rc} {out[:120]} {err[:200]}")
    if rc != 0:
        return
    try:
        sectors_512 = int(out.strip())
    except ValueError:
        return
    if sectors_512 <= 65536:
        return
    seek = max(0, sectors_512 - 65536)
    _append_restore_cmd_evidence(
        evidence,
        [
            "dd",
            "if=/dev/zero",
            f"of={device}",
            "bs=512",
            "count=65536",
            f"seek={seek}",
            "conv=fsync",
        ],
        timeout=120,
    )


def _zap_target_disk_for_restore(device: str, evidence: list[str]) -> bool:
    _quiesce_target_disk(device, evidence)
    rc = _append_restore_cmd_evidence(evidence, ["sgdisk", "--zap-all", device], timeout=60)
    if rc == 0:
        return True

    evidence.append(
        "sgdisk zap failed; clearing stale target signatures and retrying GPT cleanup"
    )
    for part in _target_partition_paths(device):
        _append_restore_cmd_evidence(evidence, ["wipefs", "-af", part], timeout=30)
    _append_restore_cmd_evidence(evidence, ["wipefs", "-af", device], timeout=60)
    _zero_target_disk_edges(device, evidence)
    _quiesce_target_disk(device, evidence)
    return _append_restore_cmd_evidence(evidence, ["sgdisk", "--zap-all", device], timeout=60) == 0


def _target_gpt_create_command(
    table: dict,
    target_parts: list[dict],
    target_last_lba: int,
    device: str,
    preserve_guids: bool = True,
) -> tuple[list[str], str]:
    cmd = ["sgdisk", "--clear", "--set-alignment=1"]
    if preserve_guids and table.get("label-id"):
        cmd.append(f"--disk-guid={table['label-id']}")
    for part in sorted(target_parts, key=lambda item: int(item.get("number") or 0)):
        number = int(part.get("number") or 0)
        start = int(part.get("start") or 0)
        end = _part_end(part)
        if number <= 0 or start <= 0 or end <= start or end > target_last_lba:
            return [], f"precreate failed: invalid calculated partition {number}"
        cmd.append(f"--new={number}:{start}:{end}")
        cmd.extend(_sgdisk_metadata_args(number, part, preserve_guids=preserve_guids))
    cmd.append(device)
    return cmd, ""


def _create_target_gpt_with_retries(
    table: dict,
    target_parts: list[dict],
    target_last_lba: int,
    device: str,
    evidence: list[str],
) -> bool:
    if not _zap_target_disk_for_restore(device, evidence):
        return False

    attempts = [
        (True, ""),
        (True, "sgdisk create failed; retrying GPT creation after cleanup"),
        (
            False,
            "sgdisk create failed with preserved GUIDs; "
            "retrying with fresh GPT/partition GUIDs",
        ),
    ]
    for index, (preserve_guids, retry_message) in enumerate(attempts):
        if retry_message:
            evidence.append(retry_message)
            if not _zap_target_disk_for_restore(device, evidence):
                return False

        cmd, error = _target_gpt_create_command(
            table,
            target_parts,
            target_last_lba,
            device,
            preserve_guids=preserve_guids,
        )
        if error:
            evidence.append(error)
            return False
        rc = _append_restore_cmd_evidence(evidence, cmd, timeout=60, out_cap=500, err_cap=500)
        if rc == 0:
            if not preserve_guids:
                evidence.append("precreated target GPT with fresh disk/partition GUIDs")
            return True
        if index == len(attempts) - 1:
            break
    return False


def _prepare_target_windows_gpt_from_image(
    image_dir: str,
    device: str,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> tuple[bool, bool, str]:
    """Pre-create a target-sized Windows GPT before Clonezilla restore.

    Clonezilla's proportional GPT mode can fail when a 512 GB Windows image is
    restored to a smaller, but still large enough, disk. Creating the target
    layout ourselves keeps EFI/MSR fixed, places recovery at the end, and lets
    the Windows partition fill the available space before ``ocs-sr`` writes
    partition data.
    """
    evidence: list[str] = []
    pt_path = _find_image_sfdisk_table(image_dir)
    if not pt_path:
        return True, False, "precreate skipped: no Clonezilla -pt.sf table found"

    table, parts, parse_ev = _parse_image_sfdisk_table(pt_path)
    evidence.append(parse_ev)
    if not parts:
        return True, False, "\n".join(evidence)
    if (table.get("label") or "").lower() != "gpt":
        evidence.append("precreate skipped: image partition table is not GPT")
        return True, False, "\n".join(evidence)
    if (table.get("unit") or "sectors").lower() != "sectors":
        evidence.append("precreate skipped: image partition table unit is not sectors")
        return True, False, "\n".join(evidence)

    os_part, _fixed_parts, rec_part, layout_ev = _pick_windows_gpt_parts(parts)
    evidence.append(layout_ev)
    if not os_part:
        return True, False, "\n".join(evidence)

    target_size_bytes, size_ev = _target_disk_size_bytes(device)
    evidence.append(size_ev)
    if target_size_bytes is None:
        return False, False, "\n".join(evidence)
    source_sector_size = int(table.get("sector-size") or 512)
    target_sector_size = _logical_sector_size(device)
    evidence.append(f"sector_size image={source_sector_size} target={target_sector_size}")
    if source_sector_size <= 0 or target_sector_size <= 0:
        evidence.append("precreate failed: invalid source or target sector size")
        return False, False, "\n".join(evidence)
    target_logical_sectors = target_size_bytes // target_sector_size
    if target_logical_sectors <= 68:
        evidence.append("precreate failed: target disk is too small for GPT")
        return False, False, "\n".join(evidence)
    evidence.append(f"target_logical_sectors={target_logical_sectors}")

    target_last_lba = target_logical_sectors - 34
    rec_size = _source_sector_count_to_target_count(
        int(rec_part.get("size") or 0), source_sector_size, target_sector_size,
    )
    rec_start = _align_down(target_last_lba - rec_size + 1)
    rec_end = rec_start + rec_size - 1
    os_start = _source_sector_to_target_sector(
        int(os_part.get("start") or 0), source_sector_size, target_sector_size,
    )
    os_end = rec_start - 1
    if rec_size <= 0 or rec_end > target_last_lba or os_end <= os_start:
        evidence.append(
            "precreate failed: target disk is too small for EFI/MSR/Windows/recovery layout"
        )
        return False, False, "\n".join(evidence)

    target_parts: list[dict] = []
    os_number = int(os_part.get("number") or 0)
    rec_number = int(rec_part.get("number") or 0)
    for source in parts:
        number = int(source.get("number") or 0)
        target = dict(source)
        target["start"] = _source_sector_to_target_sector(
            int(source.get("start") or 0), source_sector_size, target_sector_size,
        )
        target["size"] = _source_sector_count_to_target_count(
            int(source.get("size") or 0), source_sector_size, target_sector_size,
        )
        if number == os_number:
            target["start"] = os_start
            target["size"] = os_end - os_start + 1
        elif number == rec_number:
            target["start"] = rec_start
            target["size"] = rec_size
        else:
            end = int(target.get("start") or 0) + int(target.get("size") or 0) - 1
            if end >= os_start and number != os_number:
                evidence.append("precreate failed: fixed partition overlaps Windows start")
                return False, False, "\n".join(evidence)
        target_parts.append(target)

    _emit_restore_stage(progress_callback, "Pre-restore: creating target Windows GPT")
    if not _create_target_gpt_with_retries(
        table,
        target_parts,
        target_last_lba,
        device,
        evidence,
    ):
        return False, False, "\n".join(evidence)

    _run(["sgdisk", "-e", device], timeout=30)
    _run(["partprobe", device], timeout=10)
    _run(["udevadm", "settle"], timeout=10)
    evidence.append(
        "precreated target GPT: "
        f"p{os_number} start={os_start} size={os_end - os_start + 1}; "
        f"p{rec_number} start={rec_start} size={rec_size}; "
        f"target_last_lba={target_last_lba}"
    )
    return True, True, "\n".join(evidence)


def _image_files_for_part(image_dir: str, part: str) -> tuple[list[str], str, str]:
    """Return Clonezilla image split files for one partition.

    The kind is the Clonezilla partclone filesystem token, such as ``ntfs``,
    ``vfat``, or ``dd``. The compression value is one of ``xz``, ``gzip``,
    ``zstd``, or ``none``.
    """
    try:
        names = os.listdir(image_dir)
    except OSError:
        return [], "", ""
    candidates = []
    prefix = f"{part}."
    for name in names:
        lower = name.lower()
        if not name.startswith(prefix):
            continue
        if lower.endswith((".md5", ".sha1", ".sha256", ".sha512", ".log", ".txt")):
            continue
        if "-ptcl-img" in lower or "-dd-img" in lower:
            candidates.append(os.path.join(image_dir, name))
    if not candidates:
        return [], "", ""
    candidates.sort()
    sample = os.path.basename(candidates[0]).lower()
    fs_match = re.match(rf"{re.escape(part.lower())}\.([a-z0-9_+\-]+)-ptcl-img", sample)
    kind = fs_match.group(1) if fs_match else "raw"
    compression = "none"
    if ".xz" in sample:
        compression = "xz"
    elif ".gz" in sample:
        compression = "gzip"
    elif ".zst" in sample or ".zstd" in sample:
        compression = "zstd"
    return candidates, kind, compression


def _partclone_log_path(part: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.+-]", "_", part or "partition")
    return f"/tmp/vstl-partclone-{safe}.log"


def _concat_stream_shell(files: list[str], mode: str = "cat") -> str:
    quoted_files = " ".join(shlex.quote(path) for path in files)
    if mode == "python":
        streamer = (
            "import shutil,sys\n"
            "out=sys.stdout.buffer\n"
            "buf=8*1024*1024\n"
            "try:\n"
            "    for p in sys.argv[1:]:\n"
            "        with open(p,'rb') as f:\n"
            "            shutil.copyfileobj(f, out, buf)\n"
            "except BrokenPipeError:\n"
            "    sys.exit(141)\n"
        )
        return f"python3 -c {shlex.quote(streamer)} {quoted_files}"
    return f"cat {quoted_files}"


def _stream_decode_shell(files: list[str], compression: str, mode: str = "cat") -> str:
    cat_cmd = _concat_stream_shell(files, mode)
    if compression == "xz":
        return f"{cat_cmd} | xz -dc"
    if compression == "gzip":
        return f"{cat_cmd} | gzip -dc"
    if compression == "zstd":
        return f"{cat_cmd} | zstd -dc"
    return cat_cmd


def _direct_files_readable(files: list[str]) -> tuple[bool, str]:
    evidence: list[str] = []
    for path in files:
        try:
            with open(path, "rb") as handle:
                handle.read(1)
        except OSError as exc:
            evidence.append(f"{path}: {exc}")
    if evidence:
        return False, "; ".join(evidence)
    return True, f"{len(files)} image split file(s) readable"


def _ensure_direct_files_readable(
    files: list[str],
    nfs_host: str = "",
    nfs_share: str = "",
    mount_options: str = RESTORE_NFS_MOUNT_OPTIONS,
) -> tuple[bool, str]:
    ok, ev = _direct_files_readable(files)
    if ok:
        return True, ev
    evidence = [f"image split file read failed: {ev}"]
    if nfs_host and nfs_share:
        evidence.append("remounting image share before direct restore retry")
        umount_nfs()
        mount_ok, mount_ev = mount_nfs(nfs_host, nfs_share, mount_options)
        evidence.append(mount_ev)
        if mount_ok:
            ok, ev = _direct_files_readable(files)
            evidence.append(ev)
            if ok:
                return True, "\n".join(evidence)
    return False, "\n".join(evidence)


def _prepare_direct_restore_attempt(
    device: str,
    number: int,
    files: list[str],
    nfs_host: str = "",
    nfs_share: str = "",
    mount_options: str = RESTORE_NFS_MOUNT_OPTIONS,
) -> tuple[bool, str]:
    node_ok, node_ev = _wait_for_partition_node(device, number)
    read_ok, read_ev = _ensure_direct_files_readable(files, nfs_host, nfs_share, mount_options)
    return node_ok and read_ok, "\n".join([node_ev, read_ev])


def _wait_for_partition_node(device: str, number: int, timeout: int = 20) -> tuple[bool, str]:
    target = _part_path(device, number)
    evidence: list[str] = []
    deadline = time.monotonic() + max(1, timeout)
    while time.monotonic() < deadline:
        try:
            mode = os.stat(target).st_mode
            if stat.S_ISBLK(mode):
                return True, f"target partition node ready: {target}"
            evidence.append(f"{target} exists but is not a block device")
        except OSError as exc:
            evidence.append(f"{target} not ready: {exc}")
        _run(["partprobe", device], timeout=5)
        _run(["udevadm", "settle"], timeout=5)
        time.sleep(0.5)
    return False, "; ".join(evidence[-4:])


def _run_direct_restore_command(
    shell_body: str,
    state: dict,
    image_dir: str,
    progress_callback: Optional[Callable[[dict], None]],
    started: float,
    speed_state: dict,
    timeout: int,
) -> tuple[bool, str]:
    wrapped_shell = f"set -o pipefail; {shell_body}"
    evidence_lines = [f"$ bash -c {shlex.quote(wrapped_shell)}"]
    try:
        proc = subprocess.Popen(
            ["bash", "-c", wrapped_shell],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=True,
        )
    except FileNotFoundError:
        return False, "bash: not found"
    except OSError as exc:
        return False, f"OSError: {exc}"

    buffer = ""
    last_emit = 0.0
    timed_out = False
    while True:
        now = time.monotonic()
        if now - started > timeout:
            timed_out = True
            evidence_lines.append(f"direct restore timeout after {timeout}s")
            _terminate_restore_process(proc)
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
                    if len(evidence_lines) > 300:
                        evidence_lines = evidence_lines[-300:]
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
        _update_capture_state_from_line(buffer.strip(), state)
    rc_value = proc.wait() if not timed_out else 124
    evidence_lines.append(f"rc={rc_value}")
    _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)
    return rc_value == 0, "\n".join(evidence_lines)[-_EVIDENCE_CAP:]


def _partclone_restore_shell(
    stream: str,
    kind: str,
    target: str,
    part: str,
    ignore_crc: bool = False,
    generic_restore: bool = False,
) -> str:
    log_path = _partclone_log_path(part)
    q_log = shlex.quote(log_path)
    q_target = shlex.quote(target)
    crc_arg = " --ignore_crc" if ignore_crc else ""
    if kind in {"raw", "dd"}:
        restore_cmd = f"dd of={q_target} bs=16M conv=fsync status=none"
    elif generic_restore:
        restore_cmd = f"partclone.restore -C{crc_arg} -L {q_log} -s - -o {q_target}"
    else:
        tool = "partclone." + re.sub(r"[^A-Za-z0-9_+.-]", "", kind)
        restore_cmd = f"{tool} -C{crc_arg} -L {q_log} -s - -r -o {q_target}"
    return (
        f"rm -f {q_log}; "
        f"{stream} | {restore_cmd}; "
        "rc=$?; "
        f"if [ $rc -ne 0 ]; then echo '--- partclone log {part} ---'; "
        f"tail -120 {q_log} 2>/dev/null || true; fi; "
        "exit $rc"
    )


def _direct_partclone_restore(
    image_dir: str,
    device: str,
    progress_callback: Optional[Callable[[dict], None]] = None,
    timeout: int = 4 * 3600,
    nfs_host: str = "",
    nfs_share: str = "",
    mount_options: str = RESTORE_NFS_MOUNT_OPTIONS,
) -> tuple[bool, str]:
    """Fallback restore path that bypasses Clonezilla's restoredisk wrapper.

    It still uses the Clonezilla/partclone image files, but streams each
    partition directly into the target partition that we created from the image
    GPT metadata. This is reserved for the repeated false "broken partition"
    condition seen in the Clonezilla wrapper.
    """
    started = time.monotonic()
    speed_state: dict = {}
    evidence: list[str] = []
    prep_ok, precreated, prep_ev = _prepare_target_windows_gpt_from_image(
        image_dir, device, progress_callback
    )
    evidence.append(prep_ev)
    if not prep_ok or not precreated:
        evidence.append("direct restore unavailable: target GPT could not be precreated")
        return False, "\n".join(evidence)[-_EVIDENCE_CAP:]

    parts = _parts_from_image_dir(image_dir)
    if not parts:
        return False, "\n".join([*evidence, "direct restore unavailable: image parts file is empty"])[-_EVIDENCE_CAP:]

    state = {
        "operation": "restoring",
        "phase": "direct partition restore",
        "elapsed_sec": 0,
        "image_dir": image_dir,
        "device": device,
        "parts": parts,
        "parts_total": len(parts),
        "completed_partitions": [],
        "partition_percent": 0.0,
        "overall_percent": 0.0,
        "last_line": "Direct partition restore fallback started",
    }
    _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)

    for part in parts:
        number = _part_number(part)
        if number is None:
            return False, "\n".join([*evidence, f"direct restore failed: cannot parse partition number from {part}"])[-_EVIDENCE_CAP:]
        target = _part_path(device, number)
        files, kind, compression = _image_files_for_part(image_dir, part)
        if not files:
            return False, "\n".join([*evidence, f"direct restore failed: no image files for {part}"])[-_EVIDENCE_CAP:]

        ready_ok, ready_ev = _prepare_direct_restore_attempt(
            device, number, files, nfs_host, nfs_share, mount_options
        )
        evidence.append(f"--- direct restore readiness {part} -> {target} ---\n{ready_ev}")
        if not ready_ok:
            return False, "\n".join(evidence)[-_EVIDENCE_CAP:]

        state["current_partition"] = part
        state["partition_index"] = parts.index(part) + 1
        state["partition_percent"] = 0.0
        state["phase"] = f"direct restoring {part}"
        state["last_line"] = f"Direct restoring {part} to {target}"
        _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)

        stream = _stream_decode_shell(files, compression)
        shell_body = _partclone_restore_shell(stream, kind, target, part)
        ok, ev = _run_direct_restore_command(
            shell_body,
            state,
            image_dir,
            progress_callback,
            started,
            speed_state,
            timeout,
        )
        if not ok and _restore_failure_allows_direct_read_retry(ev):
            evidence.append(
                f"direct restore {part}: image read/path error detected; "
                "remounting and retrying with Python split-file streamer"
            )
            ready_ok, ready_ev = _prepare_direct_restore_attempt(
                device, number, files, nfs_host, nfs_share, mount_options
            )
            evidence.append(f"--- direct restore retry readiness {part} -> {target} ---\n{ready_ev}")
            if ready_ok:
                state["last_line"] = f"Retrying {part} after image read/path check"
                _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)
                stream = _stream_decode_shell(files, compression, mode="python")
                shell_body = _partclone_restore_shell(stream, kind, target, part, generic_restore=True)
                ok, ev = _run_direct_restore_command(
                    shell_body,
                    state,
                    image_dir,
                    progress_callback,
                    started,
                    speed_state,
                    timeout,
                )
        if not ok and kind not in {"raw", "dd"} and _restore_failure_allows_crc_salvage(ev):
            evidence.append(f"direct restore {part}: CRC/broken image detected; retrying with --ignore_crc")
            state["last_line"] = f"Retrying {part} without Partclone CRC check"
            _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)
            shell_body = _partclone_restore_shell(stream, kind, target, part, ignore_crc=True)
            ok, ev = _run_direct_restore_command(
                shell_body,
                state,
                image_dir,
                progress_callback,
                started,
                speed_state,
                timeout,
            )
            if not ok and _restore_failure_allows_direct_read_retry(ev):
                evidence.append(
                    f"direct restore {part}: image read/path error during CRC-salvage retry; "
                    "remounting and retrying with Python split-file streamer"
                )
                ready_ok, ready_ev = _prepare_direct_restore_attempt(
                    device, number, files, nfs_host, nfs_share, mount_options
                )
                evidence.append(f"--- direct restore CRC retry readiness {part} -> {target} ---\n{ready_ev}")
                if ready_ok:
                    state["last_line"] = f"Retrying {part} after image read/path check"
                    _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)
                    stream = _stream_decode_shell(files, compression, mode="python")
                    shell_body = _partclone_restore_shell(
                        stream,
                        kind,
                        target,
                        part,
                        ignore_crc=True,
                        generic_restore=True,
                    )
                    ok, ev = _run_direct_restore_command(
                        shell_body,
                        state,
                        image_dir,
                        progress_callback,
                        started,
                        speed_state,
                        timeout,
                    )
        evidence.append(f"--- direct restore {part} -> {target} ---\n{ev}")
        if not ok:
            return False, "\n".join(evidence)[-_EVIDENCE_CAP:]
        completed = state.setdefault("completed_partitions", [])
        if part not in completed:
            completed.append(part)
        state["partition_percent"] = 100.0
        state["last_line"] = f"Finished direct restore {part}"
        _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)

    for cmd in (["partprobe", device], ["udevadm", "settle"], ["sync"]):
        rc, out, err = _run(cmd, timeout=60)
        evidence.append(f"$ {' '.join(cmd)} rc={rc} {out[:200]} {err[:300]}")
    state["phase"] = "direct partition restore completed"
    state["last_line"] = "Direct partition restore completed; verifying restored disk"
    state["partition_percent"] = 100.0
    state["overall_percent"] = 100.0
    _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)
    return True, "\n".join(evidence)[-_EVIDENCE_CAP:]


def _pick_restore_temp_path(size_bytes: int, partition_number: int) -> tuple[str, str]:
    candidates = [
        "/run/vstl-restore-work",
        "/tmp/vstl-restore-work",
        "/var/tmp/vstl-restore-work",
        os.path.join(_NFS_MOUNT_POINT, ".vstl-restore-work"),
    ]
    need = size_bytes + 128 * 1024 * 1024
    for directory in candidates:
        try:
            os.makedirs(directory, exist_ok=True)
            if shutil.disk_usage(directory).free >= need:
                return (
                    os.path.join(directory, f"recovery-p{partition_number}.img"),
                    f"temp={directory}",
                )
        except OSError:
            continue
    return "", f"no temp space for {size_bytes} byte recovery partition"


def _grow_ntfs(partition: str,
               progress_callback: Optional[Callable[[dict], None]] = None) -> tuple[bool, str]:
    _emit_restore_stage(progress_callback, f"Post-restore: expanding NTFS on {partition}")
    quoted = shlex.quote(partition)
    rc, out, err = _run(
        ["bash", "-lc", f"yes | ntfsresize -f -x {quoted}"],
        timeout=2 * 3600,
    )
    return rc == 0, f"$ ntfsresize {partition} rc={rc} {out[-500:]} {err[-500:]}"


def _expand_restored_windows_layout(
    device: str,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> tuple[bool, str]:
    """Expand restored Windows images so larger target disks do not keep
    trailing unallocated space. Common Clonezilla images restore as:
    EFI, MSR, Windows, Recovery. The recovery partition blocks growing C:,
    so for small WinRE partitions we preserve it, recreate it at the end
    of the target disk, then grow the Windows NTFS partition.
    """
    evidence: list[str] = []
    _emit_restore_stage(progress_callback, "Post-restore: checking target disk layout")
    trailing_free, free_ev = _trailing_free_bytes(device)
    evidence.append(free_ev)
    if trailing_free is None:
        return False, "\n".join(evidence)
    if trailing_free <= _MAX_ALLOWED_TRAILING_FREE_BYTES:
        evidence.append("restore layout already uses target disk")
        return True, "\n".join(evidence)

    layout = validate_partition_layout(device)
    if not layout.get("ok", False):
        evidence.append("layout issue before expansion: " + "; ".join(layout.get("issues") or []))
        return False, "\n".join(evidence)

    os_path = layout.get("os_partition") or ""
    os_num = _part_number(os_path)
    if not os_path or os_num is None:
        evidence.append("no expandable Windows partition found")
        return False, "\n".join(evidence)

    table, parts, table_ev = _read_sfdisk_table(device)
    evidence.append(table_ev)
    if not table or (table.get("label") or "").lower() != "gpt":
        evidence.append("automatic restore expansion currently supports GPT disks only")
        return False, "\n".join(evidence)

    os_entry = _entry_for_path(parts, os_path)
    if not os_entry:
        evidence.append(f"Windows partition not found in sfdisk table: {os_path}")
        return False, "\n".join(evidence)

    last_lba = int(table.get("lastlba") or 0)
    sector_size = _logical_sector_size(device)
    os_start = int(os_entry.get("start") or 0)
    os_end = _part_end(os_entry)
    recovery_entries: list[tuple[dict, dict]] = []
    for row in layout.get("partitions") or []:
        if row.get("role") != "recovery":
            continue
        entry = _entry_for_path(parts, row.get("path") or "")
        if entry and int(entry.get("start") or 0) > os_end:
            recovery_entries.append((row, entry))
    recovery_entries.sort(key=lambda item: int(item[1].get("start") or 0))

    _emit_restore_stage(progress_callback, "Post-restore: repairing GPT backup header")
    _run(["sgdisk", "-e", device], timeout=30)
    if not recovery_entries:
        _emit_restore_stage(progress_callback, "Post-restore: expanding Windows partition")
        new_os_end = last_lba - _RESTORE_LAYOUT_ALIGN_SECTORS
        if new_os_end <= os_end:
            evidence.append("no usable free space after Windows partition")
            return False, "\n".join(evidence)
        cmd = [
            "sgdisk",
            f"--delete={os_num}",
            f"--new={os_num}:{os_start}:{new_os_end}",
            *_sgdisk_metadata_args(os_num, os_entry),
            device,
        ]
        rc, out, err = _run(cmd, timeout=60)
        evidence.append(f"$ {' '.join(cmd)} rc={rc} {out[:300]} {err[:300]}")
        if rc != 0:
            return False, "\n".join(evidence)
        _run(["partprobe", device], timeout=10)
        _run(["udevadm", "settle"], timeout=10)
        ntfs_ok, ntfs_ev = _grow_ntfs(os_path, progress_callback)
        evidence.append(ntfs_ev)
        if not ntfs_ok:
            return False, "\n".join(evidence)
        return True, "\n".join(evidence)

    _rec_row, rec_entry = recovery_entries[0]
    rec_num = _part_number(rec_entry.get("node") or "")
    if rec_num is None:
        evidence.append("recovery partition number could not be determined")
        return False, "\n".join(evidence)
    rec_size = int(rec_entry.get("size") or 0)
    rec_size_bytes = rec_size * sector_size
    if rec_size_bytes > _MAX_RECOVERY_MOVE_BYTES:
        evidence.append(f"recovery partition too large to auto-move: {rec_size_bytes} bytes")
        return False, "\n".join(evidence)

    new_rec_start = _align_down(last_lba - rec_size + 1)
    new_rec_end = new_rec_start + rec_size - 1
    new_os_end = new_rec_start - _RESTORE_LAYOUT_ALIGN_SECTORS
    if new_rec_end > last_lba or new_os_end <= os_end:
        evidence.append("calculated expanded layout is not usable")
        return False, "\n".join(evidence)

    rec_path = rec_entry.get("node") or _part_path(device, rec_num)
    temp_path, temp_ev = _pick_restore_temp_path(rec_size_bytes, rec_num)
    evidence.append(temp_ev)
    if not temp_path:
        return False, "\n".join(evidence)
    _emit_restore_stage(progress_callback, f"Post-restore: saving recovery partition {rec_path}")
    rc, out, err = _run(
        ["dd", f"if={rec_path}", f"of={temp_path}", "bs=16M", "status=none"],
        timeout=30 * 60,
    )
    evidence.append(f"$ backup recovery {rec_path} rc={rc} {out[:120]} {err[:220]}")
    if rc != 0:
        return False, "\n".join(evidence)

    _emit_restore_stage(progress_callback, "Post-restore: moving recovery partition to disk end")
    cmd = [
        "sgdisk",
        f"--delete={rec_num}",
        f"--delete={os_num}",
        f"--new={os_num}:{os_start}:{new_os_end}",
        *_sgdisk_metadata_args(os_num, os_entry),
        f"--new={rec_num}:{new_rec_start}:{new_rec_end}",
        *_sgdisk_metadata_args(rec_num, rec_entry),
        device,
    ]
    rc, out, err = _run(cmd, timeout=60)
    evidence.append(f"$ {' '.join(cmd)} rc={rc} {out[:300]} {err[:300]}")
    if rc != 0:
        return False, "\n".join(evidence)
    _run(["partprobe", device], timeout=10)
    _run(["udevadm", "settle"], timeout=10)
    rec_new_path = _part_path(device, rec_num)
    _emit_restore_stage(progress_callback, f"Post-restore: restoring recovery partition {rec_new_path}")
    rc, out, err = _run(
        ["dd", f"if={temp_path}", f"of={rec_new_path}", "bs=16M", "conv=fsync", "status=none"],
        timeout=30 * 60,
    )
    evidence.append(f"$ restore recovery {rec_new_path} rc={rc} {out[:120]} {err[:220]}")
    try:
        os.remove(temp_path)
    except OSError:
        pass
    if rc != 0:
        return False, "\n".join(evidence)
    ntfs_ok, ntfs_ev = _grow_ntfs(os_path, progress_callback)
    evidence.append(ntfs_ev)
    if not ntfs_ok:
        return False, "\n".join(evidence)
    _emit_restore_stage(progress_callback, "Post-restore: finalizing partition table")
    _run(["sgdisk", "-e", device], timeout=30)
    _run(["partprobe", device], timeout=10)
    _run(["udevadm", "settle"], timeout=10)
    return True, "\n".join(evidence)


def _verify_restore(
    device: str,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> tuple[bool, str]:
    """Run ``partprobe`` + ``lsblk`` after the restore and confirm a fresh
    partition table is visible. This isn't cryptographic verification â€”
    Clonezilla's own partclone phase verifies block hashes â€” but it gives
    the operator a quick visual sanity check on the result.
    """
    # A smaller GPT image restored to a larger drive can leave the backup GPT
    # header at the old end of disk. Repair that before checking usable space.
    _emit_restore_stage(progress_callback, "Post-restore: verifying restored partitions")
    _run(["sgdisk", "-e", device], timeout=30)
    _run(["partprobe", device], timeout=10)
    _run(["udevadm", "settle"], timeout=10)
    rc, out, err = _run(
        ["lsblk", "-no", "NAME,SIZE,TYPE,FSTYPE,LABEL", device], timeout=10,
    )
    if rc != 0:
        return False, f"lsblk rc={rc} err={err[:200]}"
    has_part = any(line.split() and "part" in line.split() for line in out.splitlines())
    evidence = [out[:2000]]
    if not has_part:
        evidence.append("no restored partitions detected")
        return False, "\n".join(evidence)

    expanded, expand_ev = _expand_restored_windows_layout(device, progress_callback)
    evidence.append("$ post-restore layout expansion")
    evidence.append(expand_ev)
    if expanded:
        _run(["partprobe", device], timeout=10)
        _run(["udevadm", "settle"], timeout=10)
        rc, out, err = _run(
            ["lsblk", "-no", "NAME,SIZE,TYPE,FSTYPE,LABEL", device], timeout=10,
        )
        evidence.append(out[:2000] if rc == 0 else f"post-expand lsblk rc={rc} err={err[:200]}")

    layout = validate_partition_layout(device)
    if not layout.get("ok", False):
        evidence.append("layout issue: " + "; ".join(layout.get("issues") or []))
        return False, "\n".join(evidence)[:2000]

    trailing_free, free_ev = _trailing_free_bytes(device)
    evidence.append(free_ev)
    if trailing_free is not None and trailing_free > _MAX_ALLOWED_TRAILING_FREE_BYTES:
        evidence.append(
            "restore left too much unallocated space after the last partition; "
            "destination layout was not expanded to disk size"
        )
        return False, "\n".join(evidence)[:2000]
    _emit_restore_stage(progress_callback, "Post-restore: verification complete")
    return True, "\n".join(evidence)[:2000]


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
    mount_options: str = RESTORE_NFS_MOUNT_OPTIONS,
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
        image_subdir,
        device,
        image_dir=image_dir,
        progress_callback=progress_callback,
        nfs_host=nfs_host,
        nfs_share=nfs_share,
        mount_options=mount_options,
    )
    duration = int(time.monotonic() - started_ts)
    if not ok_r:
        failure_summary = _restore_failure_summary(restore_ev)
        error_message = "ocs-sr restoredisk failed"
        if failure_summary:
            error_message = f"{error_message}: {failure_summary}"
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
            "error_message": error_message,
        }

    _emit_restore_stage(progress_callback, "Clonezilla finished; verifying restored disk")
    verified, verify_ev = _verify_restore(device, progress_callback)
    restore_ok = bool(verified)
    firmware_trigger = (
        install_post_restore_firmware_trigger(device)
        if restore_ok else
        {
            "ok": False,
            "installed": False,
            "evidence": "skipped because post-restore partition layout verification failed",
        }
    )
    return {
        "ok": restore_ok,
        "result": "SUCCESS" if restore_ok else "FAIL",
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
        "error_message": "" if restore_ok else "post-restore partition layout verification failed",
    }
