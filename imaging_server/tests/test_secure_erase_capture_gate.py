import importlib.util
import json
from pathlib import Path
import tempfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CAPTURE_PATH = ROOT / "bench-client" / "vstl_image_capture.py"
ERASE_PATH = ROOT / "bench-client" / "vstl_secure_erase.py"
TUI = (ROOT / "bench-client" / "vstl-imaging-tui.py").read_text(encoding="utf-8")
DEPLOY = (ROOT / "tools" / "deploy_bench_client_live.sh").read_text(encoding="utf-8")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


capture = _load_module("vstl_image_capture_gate", CAPTURE_PATH)
erase = _load_module("vstl_secure_erase_identity", ERASE_PATH)


def _drive(**overrides):
    values = {
        "device": "/dev/nvme0n1",
        "device_type": "NVMe",
        "device_model": "Example NVMe",
        "device_serial": "SSD-SERIAL-001",
        "device_wwn": "eui.0011223344556677",
        "device_size_bytes": 256_060_514_304,
    }
    values.update(overrides)
    return values


def _successful_erase(**overrides):
    values = {
        "ok": True,
        "verified": True,
        "method": "NVMe_SANITIZE_BLOCK_ERASE",
        "started_at": "2026-06-06T10:00:00+00:00",
        "completed_at": "2026-06-06T10:05:00+00:00",
    }
    values.update(overrides)
    return values


def test_verified_erase_authorizes_only_the_exact_laptop_and_drive(tmp_path):
    written = capture.write_secure_erase_record(
        "LAPTOP-SERIAL-A",
        _drive(),
        _successful_erase(),
        repository_root=str(tmp_path),
    )
    assert written["ok"]

    allowed = capture.check_secure_erase_record(
        "LAPTOP-SERIAL-A", _drive(), repository_root=str(tmp_path)
    )
    assert allowed["ok"]

    wrong_laptop = capture.check_secure_erase_record(
        "LAPTOP-SERIAL-B", _drive(), repository_root=str(tmp_path)
    )
    assert not wrong_laptop["ok"]

    wrong_drive = capture.check_secure_erase_record(
        "LAPTOP-SERIAL-A",
        _drive(device_wwn="eui.8899", device_serial="SSD-SERIAL-002"),
        repository_root=str(tmp_path),
    )
    assert not wrong_drive["ok"]


def test_capture_gate_falls_back_to_serial_when_wwn_is_missing_later(tmp_path):
    written = capture.write_secure_erase_record(
        "LAPTOP-SERIAL-A",
        _drive(device_serial="SSD-SERIAL-001", device_wwn="eui.0011223344556677"),
        _successful_erase(),
        repository_root=str(tmp_path),
    )
    assert written["ok"]

    allowed = capture.check_secure_erase_record(
        "LAPTOP-SERIAL-A",
        _drive(device_serial="SSD-SERIAL-001", device_wwn=""),
        repository_root=str(tmp_path),
    )
    assert allowed["ok"]
    assert allowed["matched_by"] == "drive_serial"
    assert allowed["expected_path"] != allowed["path"]


def test_capture_gate_falls_back_to_wwn_when_serial_is_missing_later(tmp_path):
    written = capture.write_secure_erase_record(
        "LAPTOP-SERIAL-A",
        _drive(device_serial="SSD-SERIAL-001", device_wwn="eui.0011223344556677"),
        _successful_erase(),
        repository_root=str(tmp_path),
    )
    assert written["ok"]

    allowed = capture.check_secure_erase_record(
        "LAPTOP-SERIAL-A",
        _drive(device_serial="", device_wwn="eui.0011223344556677"),
        repository_root=str(tmp_path),
    )
    assert allowed["ok"]
    assert allowed["matched_by"] in {"drive_fingerprint", "stable_drive_id", "drive_wwn"}


def test_partition_layout_allows_missing_winre_as_warning(monkeypatch):
    disk = {
        "path": "/dev/nvme0n1",
        "type": "disk",
        "children": [
            {
                "path": "/dev/nvme0n1p1",
                "type": "part",
                "size": 104_857_600,
                "fstype": "vfat",
                "parttype": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
                "partlabel": "EFI System Partition",
            },
            {
                "path": "/dev/nvme0n1p2",
                "type": "part",
                "size": 16_777_216,
                "parttype": "e3c9e316-0b5c-4db8-817d-f92df00215ae",
                "partlabel": "Microsoft reserved partition",
            },
            {
                "path": "/dev/nvme0n1p3",
                "type": "part",
                "size": 250_000_000_000,
                "fstype": "ntfs",
                "label": "Windows",
            },
        ],
    }
    monkeypatch.setattr(capture, "_find_disk_tree", lambda device: (True, disk, ""))

    layout = capture.validate_partition_layout("/dev/nvme0n1")

    assert layout["ok"]
    assert layout["issues"] == []
    assert "No Windows recovery/WinRE partition was detected." in layout["warnings"]
    assert layout["os_partition"] == "/dev/nvme0n1p3"
    assert layout["recovery_count"] == 0


def test_partition_layout_allows_dell_factory_oem_ntfs_partitions(monkeypatch):
    disk = {
        "path": "/dev/nvme0n1",
        "type": "disk",
        "children": [
            {
                "path": "/dev/nvme0n1p1",
                "type": "part",
                "size": 104_857_600,
                "fstype": "vfat",
                "parttype": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
                "partlabel": "EFI System Partition",
            },
            {
                "path": "/dev/nvme0n1p2",
                "type": "part",
                "size": 16_777_216,
                "parttype": "e3c9e316-0b5c-4db8-817d-f92df00215ae",
                "partlabel": "Microsoft reserved partition",
            },
            {
                "path": "/dev/nvme0n1p3",
                "type": "part",
                "size": 480_000_000_000,
                "fstype": "ntfs",
                "label": "OS",
            },
            {
                "path": "/dev/nvme0n1p4",
                "type": "part",
                "size": 16_000_000_000,
                "fstype": "ntfs",
                "label": "Image",
            },
            {
                "path": "/dev/nvme0n1p5",
                "type": "part",
                "size": 1_500_000_000,
                "fstype": "ntfs",
                "label": "DELLSUPPORT",
            },
        ],
    }
    monkeypatch.setattr(capture, "_find_disk_tree", lambda device: (True, disk, ""))

    layout = capture.validate_partition_layout("/dev/nvme0n1")

    assert layout["ok"]
    assert layout["issues"] == []
    assert layout["os_partition"] == "/dev/nvme0n1p3"
    assert layout["recovery_count"] == 2


def test_partition_layout_still_blocks_extra_partitions(monkeypatch):
    disk = {
        "path": "/dev/nvme0n1",
        "type": "disk",
        "children": [
            {"path": "/dev/nvme0n1p1", "type": "part", "size": 250_000_000_000, "fstype": "ntfs"},
            {
                "path": "/dev/nvme0n1p2",
                "type": "part",
                "size": 20_000_000_000,
                "fstype": "ext4",
                "label": "Data",
            },
        ],
    }
    monkeypatch.setattr(capture, "_find_disk_tree", lambda device: (True, disk, ""))

    layout = capture.validate_partition_layout("/dev/nvme0n1")

    assert not layout["ok"]
    assert any(issue.startswith("Extra unsupported partition") for issue in layout["issues"])


def test_secure_erase_record_keeps_local_certificate_details(tmp_path):
    certificate = {
        "certificate_id": "SE-20260619-ABCDEF1234567890",
        "verification_hash": "abc123",
        "wipe_standard": "NIST SP 800-88 Purge",
        "certificate_status": "issued",
        "remote_post_ok": False,
        "remote_error": "HTTP 422",
    }

    written = capture.write_secure_erase_record(
        "LAPTOP-SERIAL-A",
        _drive(),
        _successful_erase(method="ATA_SECURITY_ERASE"),
        certificate=certificate,
        certificate_ok=True,
        repository_root=str(tmp_path),
    )

    assert written["ok"]
    record = written["record"]
    assert record["certificate_id"] == certificate["certificate_id"]
    assert record["verification_hash"] == certificate["verification_hash"]
    assert record["wipe_standard"] == "NIST SP 800-88 Purge"
    assert record["remote_post_ok"] is False
    assert record["certificate"]["remote_error"] == "HTTP 422"

    allowed = capture.check_secure_erase_record(
        "LAPTOP-SERIAL-A", _drive(), repository_root=str(tmp_path)
    )
    assert allowed["ok"]
    assert allowed["record"]["certificate_id"] == certificate["certificate_id"]


def test_unverified_erase_does_not_create_capture_authorization(tmp_path):
    result = capture.write_secure_erase_record(
        "LAPTOP-SERIAL-A",
        _drive(),
        _successful_erase(verified=False),
        repository_root=str(tmp_path),
    )
    assert not result["ok"]
    assert not list(tmp_path.rglob("*.json"))


def test_missing_storage_identity_blocks_authorization(tmp_path):
    identity = capture.secure_erase_gate_identity(
        "LAPTOP-SERIAL-A", _drive(device_wwn="", device_serial="")
    )
    assert not identity["ok"]
    assert "exact drive cannot be verified" in " ".join(identity["issues"])


def test_capture_refuses_to_mount_or_start_without_authorization():
    with mock.patch.object(capture, "mount_nfs") as mount_nfs:
        result = capture.run_capture(
            device="/dev/nvme0n1",
            brand="Dell",
            model="Latitude",
            part_number="ABC",
            source_serial="LAPTOP-SERIAL-A",
            nfs_host="10.255.0.75",
            nfs_share="/images/dev",
            secure_erase_authorized=False,
        )
    assert not result["ok"]
    assert "secure-erase authorization" in result["error_message"]
    mount_nfs.assert_not_called()


def test_drive_detection_returns_serial_wwn_and_exact_byte_capacity():
    lsblk = {
        "blockdevices": [
            {
                "name": "nvme0n1",
                "path": "/dev/nvme0n1",
                "type": "disk",
                "rm": 0,
                "rota": 0,
                "size": 256_060_514_304,
                "model": "Example NVMe",
                "tran": "nvme",
                "serial": "SSD-SERIAL-001",
                "wwn": "eui.0011223344556677",
            }
        ]
    }
    with mock.patch.object(erase, "_run", return_value=(0, json.dumps(lsblk), "")):
        drive = erase.detect_primary_drive()
    assert drive["device_serial"] == "SSD-SERIAL-001"
    assert drive["device_wwn"] == "eui.0011223344556677"
    assert drive["device_size_bytes"] == 256_060_514_304


def test_nvme_refuses_user_data_format_when_crypto_format_fails():
    calls = []

    def crypto_fail(device):
        calls.append("crypto")
        return False, "NVMe_FORMAT_CRYPTO", "crypto format rejected"

    with (
        mock.patch.object(erase, "_release_block_device", return_value="released"),
        mock.patch.object(erase, "_nvme_sanitize_actions", return_value=([], "sanicap=0x00000000")),
        mock.patch.object(erase, "_nvme_format_crypto", side_effect=crypto_fail),
        mock.patch.object(erase, "_nvme_format_user_data") as user_data_format,
        mock.patch.object(erase, "_nvme_secure_discard_clear") as secure_discard,
        mock.patch.object(erase, "_software_zero_clear") as software_zero,
        mock.patch.object(erase, "_verify_wipe_sample") as verify_sample,
    ):
        result = erase.run_secure_erase(_drive())

    assert not result["ok"]
    assert result["method"] == "NVMe_FORMAT_CRYPTO"
    assert "Clear fallback methods are disabled" in result["error_message"]
    assert "NVMe_FORMAT_USER_DATA" in result["evidence"]
    assert calls == ["crypto"]
    user_data_format.assert_not_called()
    secure_discard.assert_not_called()
    software_zero.assert_not_called()
    verify_sample.assert_not_called()


def test_nvme_format_retries_controller_namespace_variant():
    calls = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        if cmd == ["nvme", "format", "/dev/nvme0n1", "-s", "1", "-f"]:
            return 1, "", "namespace path rejected"
        if cmd == ["nvme", "format", "/dev/nvme0", "-n", "1", "-s", "1", "-f"]:
            return 0, "Success formatting namespace:1\n", ""
        raise AssertionError(f"unexpected command: {cmd}")

    with mock.patch.object(erase, "_run", side_effect=fake_run):
        ok, method, evidence = erase._nvme_format_user_data("/dev/nvme0n1")

    assert ok
    assert method == "NVMe_FORMAT_USER_DATA"
    assert "namespace path rejected" in evidence
    assert calls == [
        ["nvme", "format", "/dev/nvme0n1", "-s", "1", "-f"],
        ["nvme", "format", "/dev/nvme0", "-n", "1", "-s", "1", "-f"],
    ]


def test_nvme_refuses_secure_discard_clear_when_native_purge_methods_fail():
    with (
        mock.patch.object(erase, "_release_block_device", return_value="released"),
        mock.patch.object(erase, "_nvme_sanitize_actions", return_value=([], "sanicap=0x00000000")),
        mock.patch.object(erase, "_nvme_format_crypto", return_value=(False, "NVMe_FORMAT_CRYPTO", "crypto rejected")),
        mock.patch.object(erase, "_nvme_format_user_data") as user_data_format,
        mock.patch.object(erase, "_nvme_secure_discard_clear") as secure_discard,
        mock.patch.object(erase, "_software_zero_clear") as software_zero,
        mock.patch.object(erase, "_verify_wipe_sample") as verify_sample,
    ):
        result = erase.run_secure_erase(_drive())

    assert not result["ok"]
    assert "NVMe_SECURE_DISCARD_CLEAR" in result["evidence"]
    user_data_format.assert_not_called()
    secure_discard.assert_not_called()
    software_zero.assert_not_called()
    verify_sample.assert_not_called()


def test_nvme_refuses_software_zero_clear_when_controller_rejects_everything():
    with (
        mock.patch.object(erase, "_release_block_device", return_value="released"),
        mock.patch.object(erase, "_nvme_sanitize_actions", return_value=([], "sanicap=0x00000000")),
        mock.patch.object(erase, "_nvme_format_crypto", return_value=(False, "NVMe_FORMAT_CRYPTO", "crypto rejected")),
        mock.patch.object(erase, "_nvme_format_user_data") as user_data_format,
        mock.patch.object(erase, "_nvme_secure_discard_clear") as secure_discard,
        mock.patch.object(erase, "_software_zero_clear") as software_zero,
        mock.patch.object(erase, "_verify_wipe_sample") as verify_sample,
    ):
        result = erase.run_secure_erase(_drive())

    assert not result["ok"]
    assert "NVMe_SOFTWARE_ZERO_CLEAR" in result["evidence"]
    assert "tried: NVMe_FORMAT_CRYPTO" in result["error_message"]
    user_data_format.assert_not_called()
    secure_discard.assert_not_called()
    software_zero.assert_not_called()
    verify_sample.assert_not_called()


def test_software_zero_clear_overwrites_user_addressable_bytes():
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        path = Path(handle.name)
        handle.write(b"A" * 8192)
    progress = []
    try:
        ok, method, evidence = erase._software_zero_clear(
            str(path),
            progress=progress.append,
            size_bytes=8192,
            chunk_size=1024,
        )
        assert ok
        assert method == "NVMe_SOFTWARE_ZERO_CLEAR"
        assert "bytes_written=8192" in evidence
        assert path.read_bytes() == b"\x00" * 8192
        assert progress[-1]["percent"] == 100
    finally:
        path.unlink(missing_ok=True)


def test_nvme_sanitize_actions_follow_controller_capabilities():
    with mock.patch.object(
        erase,
        "_run",
        return_value=(0, json.dumps({"sanicap": 0x7}), ""),
    ):
        actions, evidence = erase._nvme_sanitize_actions("/dev/nvme0n1")

    assert actions == [2, 4, 3]
    assert "sanicap=0x00000007" in evidence


def test_nvme_sanitize_actions_include_overwrite_only_when_advertised():
    with mock.patch.object(
        erase,
        "_run",
        return_value=(0, json.dumps({"sanicap": 0x4}), ""),
    ):
        actions, evidence = erase._nvme_sanitize_actions("/dev/nvme0n1")

    assert actions == [3]
    assert "sanicap=0x00000004" in evidence


def test_nvme_sanitize_overwrite_label_is_purge_method():
    with mock.patch.object(
        erase,
        "_run",
        side_effect=[
            (0, "", ""),
            (0, "sprog : 65535\nsstat : 0x101\n", ""),
        ],
    ):
        ok, method, evidence = erase._nvme_sanitize("/dev/nvme0n1", action=3)

    assert ok
    assert method == "NVMe_SANITIZE_OVERWRITE"
    assert "$ nvme sanitize /dev/nvme0n1 -a 3" in evidence


def test_nvme_tries_sanitize_overwrite_before_format_crypto():
    calls = []

    def sanitize_fail(device, action, progress=None):
        calls.append(f"sanitize-{action}")
        return False, erase._NVME_SANITIZE_ACTION_LABELS[action], f"sanitize {action} rejected"

    def crypto_ok(device):
        calls.append("format-crypto")
        return True, "NVMe_FORMAT_CRYPTO", "crypto format accepted"

    with (
        mock.patch.object(erase, "_release_block_device", return_value="released"),
        mock.patch.object(erase, "_nvme_sanitize_actions", return_value=([3], "sanicap=0x00000004")),
        mock.patch.object(erase, "_nvme_sanitize", side_effect=sanitize_fail),
        mock.patch.object(erase, "_nvme_format_crypto", side_effect=crypto_ok),
        mock.patch.object(erase, "_verify_wipe_sample", return_value=(True, "sample clear")),
    ):
        result = erase.run_secure_erase(_drive())

    assert result["ok"]
    assert result["method"] == "NVMe_FORMAT_CRYPTO"
    assert calls == ["sanitize-3", "format-crypto"]


def test_nvme_sanitize_log_parser_accepts_compact_sstat_sprog_format():
    sstat, percent = erase._parse_nvme_sanitize_log("""
NVMe Status Log for device:nvme0n1
sprog       : 32768
sstat       : 0x102
""")

    assert sstat == 0x102
    assert percent == 50


def test_nvme_sanitize_completes_with_compact_sstat_format():
    calls = []
    progress = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        if cmd[:2] == ["nvme", "sanitize"]:
            return 0, "", ""
        if cmd[:2] == ["nvme", "sanitize-log"]:
            return 0, "sprog : 65535\nsstat : 0x101\n", ""
        raise AssertionError(f"unexpected command: {cmd}")

    with mock.patch.object(erase, "_run", side_effect=fake_run):
        ok, method, evidence = erase._nvme_sanitize(
            "/dev/nvme0n1",
            action=2,
            progress=progress.append,
        )

    assert ok
    assert method == "NVMe_SANITIZE_BLOCK_ERASE"
    assert "FINAL sanitize-log" in evidence
    assert calls == [
        ["nvme", "sanitize", "/dev/nvme0n1", "-a", "2"],
        ["nvme", "sanitize-log", "/dev/nvme0n1"],
    ]
    assert progress[0]["method"] == "NVMe_SANITIZE_BLOCK_ERASE"
    assert progress[-1]["phase"] == "completed"
    assert progress[-1]["percent"] == 100


def test_nvme_sanitize_unparseable_status_fails_quickly():
    def fake_run(cmd, timeout=60):
        if cmd[:2] == ["nvme", "sanitize"]:
            return 0, "", ""
        if cmd[:2] == ["nvme", "sanitize-log"]:
            return 0, "no parseable sanitize status here\n", ""
        raise AssertionError(f"unexpected command: {cmd}")

    with mock.patch.object(erase, "_run", side_effect=fake_run):
        ok, method, evidence = erase._nvme_sanitize(
            "/dev/nvme0n1",
            action=2,
            timeout=120,
        )

    assert not ok
    assert method == "NVMe_SANITIZE_BLOCK_ERASE"
    assert "did not expose SSTAT/status after 5 polls" in evidence


def test_clear_class_nvme_methods_are_not_certificate_standard_mapped():
    assert '"NVMe_SANITIZE_OVERWRITE": "NIST SP 800-88 Purge"' in TUI
    assert '"NVMe_FORMAT_USER_DATA": "NIST SP 800-88 Clear"' not in TUI
    assert '"NVMe_SECURE_DISCARD_CLEAR": "NIST SP 800-88 Clear"' not in TUI
    assert '"NVMe_SOFTWARE_ZERO_CLEAR": "NIST SP 800-88 Clear"' not in TUI
    assert "Unsupported data sanitization method" in TUI
    assert "Clear-class and unknown wipe methods are disabled" in TUI
    assert "def _is_certifiable_wipe_method(" in TUI
    assert 'result["certificate_status"] = "refused"' in TUI


def test_sata_ssd_refuses_blkdiscard_as_certified_secure_erase():
    drive = _drive(
        device="/dev/sda",
        device_type="SATA_SSD",
        device_model="Example SATA SSD",
        device_wwn="wwn-0x1234",
    )
    with (
        mock.patch.object(
            erase,
            "_hdparm_sanitize_erase",
            return_value=(False, "", "ATA SANITIZE unsupported"),
        ),
        mock.patch.object(
            erase,
            "_hdparm_security_erase",
            return_value=(False, "ATA_SECURITY_ERASE", "ATA command failed"),
        ),
        mock.patch.object(
            erase,
            "_blkdiscard",
            return_value=(True, "BLKDISCARD", "discard completed"),
        ) as blkdiscard,
    ):
        result = erase.run_secure_erase(drive)

    assert not result["ok"]
    assert not result["verified"]
    assert "Refused BLKDISCARD fallback" in result["evidence"]
    blkdiscard.assert_not_called()


def test_ata_security_parser_accepts_normal_not_frozen_status():
    output = """
Security:
        supported
        not enabled
        not locked
        not    frozen
"""
    assert not erase._ata_security_is_frozen(output)


def test_ata_security_parser_rejects_genuinely_frozen_status():
    output = """
Security:
        supported
        enabled
        frozen
"""
    assert erase._ata_security_is_frozen(output)


def test_ata_sanitize_parser_prefers_block_then_crypto_erase():
    output = """
Commands/features:
           *    BLOCK_ERASE_EXT command
           *    CRYPTO_SCRAMBLE_EXT command
           *    SANITIZE feature set
"""
    assert erase._ata_sanitize_methods(output) == [
        ("--sanitize-block-erase", "ATA_SANITIZE_BLOCK_ERASE"),
        ("--sanitize-crypto-scramble", "ATA_SANITIZE_CRYPTO_SCRAMBLE"),
    ]


def test_ata_sanitize_starts_background_erase_and_polls_to_success():
    identity = """
Commands/features:
           *    BLOCK_ERASE_EXT command
           *    SANITIZE feature set
"""
    calls = [
        (0, identity, ""),
        (0, "Operation started in background", ""),
        (0, "State: SD2 Sanitize operation In Process\nProgress: 0x7fff (50%)", ""),
        (0, "State: SD4 Sanitize Operation succeeded\nLast Sanitize Operation Completed Without Error", ""),
    ]
    progress = []
    with (
        mock.patch.object(erase, "_run", side_effect=calls) as run,
        mock.patch.object(erase.time, "sleep", return_value=None),
    ):
        ok, method, evidence = erase._hdparm_sanitize_erase(
            "/dev/sda", progress=progress.append,
        )

    assert ok
    assert method == "ATA_SANITIZE_BLOCK_ERASE"
    assert "--yes-i-know-what-i-am-doing" in evidence
    assert progress[-1]["percent"] == 100
    assert ["hdparm", "--sanitize-status", "/dev/sda"] in [
        call.args[0] for call in run.call_args_list
    ]


def test_ata_security_status_tracks_supported_enabled_locked_flags():
    output = """
Security:
        supported
        enabled
        not frozen
        not expired: security count
        locked
"""
    status = erase._ata_security_status(output)
    assert status == {
        "supported": True,
        "enabled": True,
        "locked": True,
        "frozen": False,
    }


def test_ata_security_erase_continues_when_drive_is_not_frozen():
    identity = "Security:\n\tsupported\n\tnot enabled\n\tnot locked\n\tnot\tfrozen\n"
    calls = [
        (0, identity, ""),
        (0, "security_password: set", ""),
        (0, "security_erase_enhanced: completed", ""),
    ]
    with mock.patch.object(erase, "_run", side_effect=calls) as run:
        ok, method, evidence = erase._hdparm_security_erase("/dev/sda")

    assert ok
    assert method == "ATA_SECURITY_ERASE_ENHANCED"
    assert "completed" in evidence
    assert run.call_count == 3


def test_ata_security_erase_retries_after_suspend_resume_unfreeze():
    frozen = "Security:\n\tsupported\n\tenabled\n\tlocked\n\tfrozen\n"
    thawed = "Security:\n\tsupported\n\tnot enabled\n\tnot locked\n\tnot frozen\n"
    calls = [
        (0, frozen, ""),
        (0, "resume completed", ""),
        (0, thawed, ""),
        (0, "security_password: set", ""),
        (0, "security_erase_enhanced: completed", ""),
    ]
    with mock.patch.object(erase, "_run", side_effect=calls) as run:
        ok, method, evidence = erase._hdparm_security_erase("/dev/sda")

    assert ok
    assert method == "ATA_SECURITY_ERASE_ENHANCED"
    assert "resume completed" in evidence
    assert "after suspend/resume" in evidence
    assert run.call_count == 5


def test_ata_security_erase_unfreezes_legacy_dell_after_sanitize_fallback():
    frozen = "Security:\n\tsupported\n\tnot enabled\n\tnot locked\n\tfrozen\n"
    thawed = "Security:\n\tsupported\n\tnot enabled\n\tnot locked\n\tnot frozen\n"
    calls = [
        (0, frozen, ""),
        (0, thawed, ""),
        (0, "security_password: set", ""),
        (0, "security_erase_enhanced: completed", ""),
    ]
    with (
        mock.patch.object(erase, "_run", side_effect=calls),
        mock.patch.object(erase, "_ata_try_unfreeze", return_value=(True, "resume completed")) as unfreeze,
        mock.patch.object(
            erase,
            "_system_dmi_profile",
            return_value="Dell Inc. | Latitude 5490 | 04/09/2025",
        ),
    ):
        ok, method, evidence = erase._hdparm_security_erase("/dev/sda")

    assert ok
    assert method == "ATA_SECURITY_ERASE_ENHANCED"
    assert "Dell Inc. | Latitude 5490" in evidence
    assert "resume completed" in evidence
    unfreeze.assert_called_once()


def test_ata_security_erase_clears_stale_enabled_state_before_arming():
    stale = "Security:\n\tsupported\n\tenabled\n\tnot locked\n\tnot frozen\n"
    cleared = "Security:\n\tsupported\n\tnot enabled\n\tnot locked\n\tnot frozen\n"
    calls = [
        (0, stale, ""),
        (0, "security_disabled", ""),
        (0, cleared, ""),
        (0, "security_password: set", ""),
        (0, "security_erase_enhanced: completed", ""),
    ]
    with mock.patch.object(erase, "_run", side_effect=calls) as run:
        ok, method, evidence = erase._hdparm_security_erase("/dev/sda")

    assert ok
    assert method == "ATA_SECURITY_ERASE_ENHANCED"
    assert "security-disable" in evidence
    commands = [call.args[0] for call in run.call_args_list]
    assert ["hdparm", "--user-master", "u", "--security-disable", "vstl", "/dev/sda"] in commands
    assert ["hdparm", "--user-master", "u", "--security-set-pass", "vstl", "/dev/sda"] in commands


def test_ata_security_erase_fails_cleanly_when_drive_is_prelocked():
    locked = "Security:\n\tsupported\n\tenabled\n\tlocked\n\tnot frozen\n"
    with mock.patch.object(erase, "_run", return_value=(0, locked, "")):
        ok, method, evidence = erase._hdparm_security_erase("/dev/sda")

    assert not ok
    assert method == "ATA_SECURITY_ERASE_ENHANCED"
    assert "enabled+locked" in evidence


def test_erase_failure_reason_explains_frozen_drive():
    reason = erase._erase_failure_reason("[drive is still FROZEN after suspend/resume]")
    assert "frozen" in reason.lower()
    assert "power off" in reason.lower()


def test_erase_failure_reason_explains_sanitize_freeze():
    reason = erase._erase_failure_reason("State: SD1 Sanitize Frozen")
    assert "sanitize" in reason.lower()
    assert "power cycle" in reason.lower()


def test_erase_failure_reason_explains_set_pass_rejection():
    evidence = (
        "$ hdparm --security-set-pass vstl /dev/sda\n"
        "rc=5\n"
        "\n"
        "SG_IO: bad/missing sense data\n"
    )
    reason = erase._erase_failure_reason(evidence)
    assert "password setup failed" in reason.lower()


def test_erase_failure_reason_explains_stale_enabled_security_state():
    reason = erase._erase_failure_reason(
        "[stale ATA security state remained enabled after security-disable attempt]"
    )
    assert "earlier ata security password state" in reason.lower()


def test_legacy_shell_client_never_certifies_blkdiscard_trim():
    legacy = (ROOT / "bench-client" / "vstl-imaging-client.sh").read_text(
        encoding="utf-8"
    )
    wipe_block = legacy.split(
        "# ---------- 5. Wipe (only if user passed --wipe) ----------", 1
    )[1].split("# ---------- 6. POST /api/imaging/ingest ----------", 1)[0]

    assert "blkdiscard -f" not in wipe_block.lower()
    assert "refusing TRIM fallback" in wipe_block
    assert 'nvme format "$PRIMARY_DISK" -s 2 -f' in wipe_block
    assert "--security-erase-enhanced" in wipe_block


def test_tui_checks_gate_before_capture_and_records_it_after_erase():
    assert "_record_secure_erase_authorization(" in TUI
    assert "_check_secure_erase_authorization(" in TUI
    assert "secure_erase_authorized=bool" in TUI
    assert "Capture  : {'AUTHORIZED'" in TUI


def test_tui_issues_local_certificate_even_when_remote_post_fails():
    assert "def _local_secure_erase_certificate(" in TUI
    assert '"certificate_id": f"SE-{date_token}-{verification_hash[:16].upper()}"' in TUI
    assert '"remote_post_ok": False' in TUI
    assert "cert_resp = _merge_secure_erase_certificate(" in TUI
    assert "cert_ok = True" in TUI
    assert "Server certificate POST failed; local certificate is saved" in TUI
    assert '"wipe_standard":      local_cert.get("wipe_standard", "")' in TUI
    assert '"verification_hash":   local_cert.get("verification_hash", "")' in TUI


def test_tui_does_not_translate_software_zero_clear_to_blkdiscard():
    assert "def _cloud_certificate_compat_wipe_method(" in TUI
    assert "bench must not translate them to BLKDISCARD" in TUI
    assert 'if method == "NVMe_SOFTWARE_ZERO_CLEAR":' not in TUI
    assert 'return "BLKDISCARD"' not in TUI
    assert 'compat_body["wipe_method"] = compat_method' in TUI
    assert 'compat_body["wipe_standard"] = _wipe_standard(compat_method)' in TUI
    assert '"remote_actual_wipe_method": str(result.get("method") or "")' in TUI
    assert "cert_resp.update(compatibility_post)" in TUI


def test_tui_posts_local_secure_erase_report_immediately():
    assert "def _post_secure_erase_local_report(" in TUI
    assert "report_source\": \"bench_secure_erase_immediate" in TUI
    assert "result[\"local_report_post_ok\"]" in TUI


def test_image_archive_never_moves_secure_erase_authorizations():
    assert "lost+found|.vstl-secure-erase" in DEPLOY
