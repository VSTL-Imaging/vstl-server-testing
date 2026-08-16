import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESTORE_PATH = ROOT / "bench-client" / "vstl_image_restore.py"
sys.path.insert(0, str(ROOT / "bench-client"))
spec = importlib.util.spec_from_file_location("vstl_image_restore_post_restore", RESTORE_PATH)
ir = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(ir)


def _latitude_5330_sfdisk_text() -> str:
    return """label: gpt
label-id: 8CAA37FC-8DB0-4663-A321-5168BE693176
device: /dev/nvme0n1
unit: sectors
first-lba: 34
last-lba: 1000215182
sector-size: 512

/dev/nvme0n1p1 : start=2048, size=409600, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, uuid=C26C5B64-8ED5-4580-9BCC-177FC7A946D7, name="Basic data partition", attrs="GUID:63"
/dev/nvme0n1p2 : start=411648, size=32768, type=E3C9E316-0B5C-4DB8-817D-F92DF00215AE, uuid=D0B30B59-6A8C-4927-85B0-0431195B34FA, name="Microsoft reserved partition", attrs="GUID:63"
/dev/nvme0n1p3 : start=444416, size=998096896, type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7, uuid=FCBF2A4D-7327-4291-999F-83D1F10592A0, name="Basic data partition"
/dev/nvme0n1p4 : start=998541312, size=1671168, type=DE94BBA4-06D1-4D40-A16A-BFD50179D6AC, uuid=7DE1045B-AD6B-4858-B4C6-A8A84C586211, attrs="RequiredPartition GUID:63"
"""


def test_post_restore_firmware_script_uses_windows_update_driver_firmware_scan():
    script = ir._post_restore_firmware_powershell()
    assert "Microsoft.Update.Session" in script
    assert "pnputil /scan-devices" in script
    assert "firmware|bios|uefi|system firmware|driver" in script
    assert "shutdown.exe /r" in script


def test_restore_uses_precreated_partition_table_mode_when_available():
    source = RESTORE_PATH.read_text(encoding="utf-8")
    command = source[source.index('cmd = ['):source.index('env = dict(os.environ)')]
    assert 'partition_mode, "-r"' in command
    assert '"-k1", "-r"' not in command
    assert 'partition_mode = "-k" if precreated_layout else "-k1"' in source


def test_restore_precreates_target_sized_windows_gpt(monkeypatch, tmp_path):
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    (image_dir / "disk").write_text("nvme0n1\n", encoding="utf-8")
    (image_dir / "nvme0n1-pt.sf").write_text(_latitude_5330_sfdisk_text(), encoding="utf-8")
    calls = []

    def fake_run(argv, timeout=30):
        calls.append(argv)
        if argv[:2] == ["blockdev", "--getsz"]:
            return 0, "500118192\n", ""
        if argv[:2] == ["blockdev", "--getss"]:
            return 0, "512\n", ""
        return 0, "", ""

    monkeypatch.setattr(ir, "_run", fake_run)

    ok, precreated, evidence = ir._prepare_target_windows_gpt_from_image(
        str(image_dir), "/dev/nvme0n1"
    )

    assert ok is True
    assert precreated is True
    assert "precreated target GPT" in evidence
    sgdisk_create = next(call for call in calls if call[:2] == ["sgdisk", "--clear"])
    assert "--set-alignment=1" in sgdisk_create
    assert "--new=1:2048:411647" in sgdisk_create
    assert "--new=2:411648:444415" in sgdisk_create
    assert "--new=3:444416:498446335" in sgdisk_create
    assert "--new=4:498446336:500117503" in sgdisk_create
    assert "--typecode=4:DE94BBA4-06D1-4D40-A16A-BFD50179D6AC" in sgdisk_create
    assert "--attributes=4:set:0" in sgdisk_create
    assert "--attributes=4:set:63" in sgdisk_create
    assert calls[0] == ["blockdev", "--getsz", "/dev/nvme0n1"]


def test_restore_precreates_512_sector_image_on_4096_sector_target(monkeypatch, tmp_path):
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    (image_dir / "disk").write_text("nvme0n1\n", encoding="utf-8")
    (image_dir / "nvme0n1-pt.sf").write_text(_latitude_5330_sfdisk_text(), encoding="utf-8")
    calls = []

    def fake_run(argv, timeout=30):
        calls.append(argv)
        if argv[:2] == ["blockdev", "--getsz"]:
            return 0, "500118192\n", ""
        if argv[:2] == ["blockdev", "--getss"]:
            return 0, "4096\n", ""
        return 0, "", ""

    monkeypatch.setattr(ir, "_run", fake_run)

    ok, precreated, evidence = ir._prepare_target_windows_gpt_from_image(
        str(image_dir), "/dev/nvme0n1"
    )

    assert ok is True
    assert precreated is True
    assert "sector_size image=512 target=4096" in evidence
    assert "target_logical_sectors=62514774" in evidence
    assert "source and target sector sizes differ" not in evidence
    sgdisk_create = next(call for call in calls if call[:2] == ["sgdisk", "--clear"])
    assert "--set-alignment=1" in sgdisk_create
    assert "--new=1:256:51455" in sgdisk_create
    assert "--new=2:51456:55551" in sgdisk_create
    assert "--new=3:55552:62304255" in sgdisk_create
    assert "--new=4:62304256:62513151" in sgdisk_create
    assert "--typecode=4:DE94BBA4-06D1-4D40-A16A-BFD50179D6AC" in sgdisk_create
    assert "--attributes=4:set:0" in sgdisk_create
    assert "--attributes=4:set:63" in sgdisk_create


def test_restore_precreate_skips_non_windows_layout(monkeypatch, tmp_path):
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    (image_dir / "disk").write_text("sda\n", encoding="utf-8")
    (image_dir / "sda-pt.sf").write_text(
        """label: dos
unit: sectors
/dev/sda1 : start=2048, size=409600, type=83
""",
        encoding="utf-8",
    )

    def fail_run(argv, timeout=30):
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(ir, "_run", fail_run)

    ok, precreated, evidence = ir._prepare_target_windows_gpt_from_image(
        str(image_dir), "/dev/sda"
    )

    assert ok is True
    assert precreated is False
    assert "not GPT" in evidence


def test_restore_retries_without_precreate_when_precreated_layout_reports_broken_image(monkeypatch):
    attempts = []
    commands = []
    events = []

    def fake_once(*args, **kwargs):
        allow_precreate = kwargs.get("allow_precreate", True)
        attempts.append(allow_precreate)
        if allow_precreate:
            return (
                False,
                "precreated target GPT\nThe image of this partition is broken: nvme0n1p3\nrc=1",
                True,
            )
        return True, "retry restore ok", False

    def fake_run(argv, timeout=30):
        commands.append(argv)
        return 0, "", ""

    monkeypatch.setattr(ir, "_ocs_restoredisk_once", fake_once)
    monkeypatch.setattr(ir, "_run", fake_run)

    ok, evidence = ir._ocs_restoredisk(
        "image", "/dev/nvme0n1", image_dir="/home/partimag/image", progress_callback=events.append,
    )

    assert ok is True
    assert attempts == [True, False]
    assert ["sgdisk", "--zap-all", "/dev/nvme0n1"] in commands
    assert "retry without precreated layout" in evidence
    assert "retry restore ok" in evidence
    assert any("retrying with image partition table" in event.get("last_line", "") for event in events)


def test_restore_uses_direct_fallback_when_clonezilla_retry_still_reports_broken(monkeypatch):
    attempts = []
    commands = []
    direct_calls = []
    events = []

    def fake_once(*args, **kwargs):
        allow_precreate = kwargs.get("allow_precreate", True)
        attempts.append(allow_precreate)
        return (
            False,
            "The image of this partition is broken: nvme0n1p3\nrc=125",
            allow_precreate,
        )

    def fake_run(argv, timeout=30):
        commands.append(argv)
        return 0, "", ""

    def fake_direct(image_dir, device, progress_callback=None, timeout=30):
        direct_calls.append((image_dir, device))
        return True, "direct restore ok"

    monkeypatch.setattr(ir, "_ocs_restoredisk_once", fake_once)
    monkeypatch.setattr(ir, "_run", fake_run)
    monkeypatch.setattr(ir, "_direct_partclone_restore", fake_direct)

    ok, evidence = ir._ocs_restoredisk(
        "image",
        "/dev/nvme0n1",
        image_dir="/home/partimag/image",
        progress_callback=events.append,
    )

    assert ok is True
    assert attempts == [True, False]
    assert direct_calls == [("/home/partimag/image", "/dev/nvme0n1")]
    assert ["sgdisk", "--zap-all", "/dev/nvme0n1"] in commands
    assert "direct partclone fallback" in evidence
    assert "direct restore ok" in evidence
    assert any("direct partition restore" in event.get("last_line", "") for event in events)


def test_precreated_restore_attempt_aborts_immediately_on_broken_image_marker(monkeypatch, tmp_path):
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    (image_dir / "parts").write_text("nvme0n1p1 nvme0n1p2 nvme0n1p3 nvme0n1p4\n", encoding="utf-8")
    original_popen = subprocess.Popen

    monkeypatch.setattr(
        ir,
        "_prepare_target_windows_gpt_from_image",
        lambda *args, **kwargs: (True, True, "precreated target GPT"),
    )

    def fake_popen(_cmd, **kwargs):
        return original_popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys,time;"
                    "print('The image of this partition is broken: nvme0n1p3', flush=True);"
                    "time.sleep(20)"
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=kwargs.get("start_new_session", False),
        )

    monkeypatch.setattr(ir.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ir.select, "select", lambda r, _w, _x, _timeout: (r, [], []))
    events = []
    started = time.monotonic()

    ok, evidence, precreated = ir._ocs_restoredisk_once(
        "image",
        "/dev/nvme0n1",
        image_dir=str(image_dir),
        progress_callback=events.append,
        timeout=30,
        allow_precreate=True,
    )

    assert ok is False
    assert precreated is True
    assert time.monotonic() - started < 5
    assert "detected broken partition marker" in evidence
    assert "rc=125" in evidence
    assert any("retrying with image partition table" in event.get("last_line", "") for event in events)


def test_non_precreated_restore_attempt_aborts_immediately_for_direct_fallback(monkeypatch, tmp_path):
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    (image_dir / "parts").write_text("nvme0n1p1 nvme0n1p2 nvme0n1p3 nvme0n1p4\n", encoding="utf-8")
    original_popen = subprocess.Popen

    def fake_popen(_cmd, **kwargs):
        return original_popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys,time;"
                    "print('The image of this partition is broken: nvme0n1p3', flush=True);"
                    "time.sleep(20)"
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=kwargs.get("start_new_session", False),
        )

    monkeypatch.setattr(ir.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ir.select, "select", lambda r, _w, _x, _timeout: (r, [], []))
    events = []
    started = time.monotonic()

    ok, evidence, precreated = ir._ocs_restoredisk_once(
        "image",
        "/dev/nvme0n1",
        image_dir=str(image_dir),
        progress_callback=events.append,
        timeout=30,
        allow_precreate=False,
    )

    assert ok is False
    assert precreated is False
    assert time.monotonic() - started < 5
    assert "detected broken partition marker" in evidence
    assert "rc=125" in evidence
    assert any("direct partition restore" in event.get("last_line", "") for event in events)


def test_direct_partclone_restore_streams_split_xz_images(monkeypatch, tmp_path):
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    (image_dir / "parts").write_text("nvme0n1p1 nvme0n1p2 nvme0n1p3\n", encoding="utf-8")
    (image_dir / "nvme0n1p1.vfat-ptcl-img.xz.aa").write_text("efi", encoding="utf-8")
    (image_dir / "nvme0n1p2.ntfs-ptcl-img.xz.aa").write_text("win-a", encoding="utf-8")
    (image_dir / "nvme0n1p2.ntfs-ptcl-img.xz.ab").write_text("win-b", encoding="utf-8")
    (image_dir / "nvme0n1p3.dd-ptcl-img.xz.aa").write_text("msr", encoding="utf-8")
    commands = []
    maintenance = []

    monkeypatch.setattr(
        ir,
        "_prepare_target_windows_gpt_from_image",
        lambda *args, **kwargs: (True, True, "precreated target GPT"),
    )

    def fake_stream(shell_body, state, image_path, progress_callback, started, speed_state, timeout):
        commands.append(shell_body)
        return True, "stream ok"

    def fake_run(argv, timeout=30):
        maintenance.append(argv)
        return 0, "", ""

    monkeypatch.setattr(ir, "_run_direct_restore_command", fake_stream)
    monkeypatch.setattr(ir, "_run", fake_run)

    ok, evidence = ir._direct_partclone_restore(str(image_dir), "/dev/nvme0n1")

    assert ok is True
    assert len(commands) == 3
    assert "xz -dc | partclone.vfat -C -L /tmp/vstl-partclone-nvme0n1p1.log -s - -r -o /dev/nvme0n1p1" in commands[0]
    assert "nvme0n1p2.ntfs-ptcl-img.xz.aa" in commands[1]
    assert "nvme0n1p2.ntfs-ptcl-img.xz.ab" in commands[1]
    assert "partclone.ntfs -C -L /tmp/vstl-partclone-nvme0n1p2.log -s - -r -o /dev/nvme0n1p2" in commands[1]
    assert "partclone.dd -C -L /tmp/vstl-partclone-nvme0n1p3.log -s - -o /dev/nvme0n1p3" in commands[2]
    assert ["partprobe", "/dev/nvme0n1"] in maintenance
    assert "precreated target GPT" in evidence


def test_restore_does_not_retry_generic_precreated_layout_failure(monkeypatch):
    attempts = []

    def fake_once(*args, **kwargs):
        attempts.append(kwargs.get("allow_precreate", True))
        return False, "precreate ok\nnetwork unreachable\nrc=1", True

    monkeypatch.setattr(ir, "_ocs_restoredisk_once", fake_once)

    ok, evidence = ir._ocs_restoredisk(
        "image", "/dev/nvme0n1", image_dir="/home/partimag/image",
    )

    assert ok is False
    assert attempts == [True]
    assert "network unreachable" in evidence


def test_restore_verification_fails_on_large_trailing_unallocated_space(monkeypatch):
    calls = []

    def fake_run(argv, timeout=30):
        calls.append(argv)
        if argv[:2] == ["lsblk", "-no"]:
            return 0, "sda 512G disk\nsda1 200M part vfat SYSTEM\nsda3 237G part ntfs Windows\nsda4 780M part ntfs Recovery", ""
        if argv[:2] == ["sfdisk", "-J"]:
            return 0, json.dumps({
                "partitiontable": {
                    "label": "gpt",
                    "lastlba": 1000215182,
                    "partitions": [
                        {"node": "/dev/sda1", "start": 2048, "size": 409600},
                        {"node": "/dev/sda2", "start": 411648, "size": 32768},
                        {"node": "/dev/sda3", "start": 444416, "size": 497025024},
                        {"node": "/dev/sda4", "start": 497469440, "size": 1597440},
                    ],
                },
            }), ""
        if argv[:2] == ["blockdev", "--getss"]:
            return 0, "512\n", ""
        return 0, "", ""

    monkeypatch.setattr(ir, "_run", fake_run)
    monkeypatch.setattr(ir, "validate_partition_layout", lambda device: {"ok": True, "issues": []})

    verified, evidence = ir._verify_restore("/dev/sda")

    assert verified is False
    assert "unallocated space" in evidence
    assert ["sgdisk", "-e", "/dev/sda"] in calls


def test_restore_expansion_moves_recovery_and_grows_windows(monkeypatch, tmp_path):
    calls = []

    table = {
        "partitiontable": {
            "label": "gpt",
            "lastlba": 1000215182,
            "partitions": [
                {"node": "/dev/sda1", "start": 2048, "size": 409600, "type": "EFI", "uuid": "1111", "name": "EFI"},
                {"node": "/dev/sda2", "start": 411648, "size": 32768, "type": "MSR", "uuid": "2222", "name": "MSR"},
                {"node": "/dev/sda3", "start": 444416, "size": 497025024, "type": "WIN", "uuid": "3333", "name": "Windows"},
                {"node": "/dev/sda4", "start": 497469440, "size": 1597440, "type": "REC", "uuid": "4444", "name": "Recovery"},
            ],
        },
    }

    def fake_run(argv, timeout=30):
        calls.append(argv)
        if argv[:2] == ["sfdisk", "-J"]:
            return 0, json.dumps(table), ""
        if argv[:2] == ["blockdev", "--getss"]:
            return 0, "512\n", ""
        return 0, "", ""

    monkeypatch.setattr(ir, "_run", fake_run)
    monkeypatch.setattr(
        ir,
        "validate_partition_layout",
        lambda device: {
            "ok": True,
            "issues": [],
            "os_partition": "/dev/sda3",
            "partitions": [
                {"path": "/dev/sda1", "role": "efi"},
                {"path": "/dev/sda2", "role": "msr"},
                {"path": "/dev/sda3", "role": "windows"},
                {"path": "/dev/sda4", "role": "recovery"},
            ],
        },
    )
    monkeypatch.setattr(
        ir,
        "_pick_restore_temp_path",
        lambda size_bytes, number: (str(tmp_path / "recovery.img"), "temp ok"),
    )

    expanded, evidence = ir._expand_restored_windows_layout("/dev/sda")

    assert expanded is True
    assert "temp ok" in evidence
    assert any(call[:1] == ["dd"] and "if=/dev/sda4" in call for call in calls)
    assert any(call[:1] == ["dd"] and "of=/dev/sda4" in call for call in calls)
    sgdisk_calls = [call for call in calls if call and call[0] == "sgdisk"]
    assert any("--delete=4" in call and "--delete=3" in call for call in sgdisk_calls)
    assert any(any(arg.startswith("--new=3:444416:") for arg in call) for call in sgdisk_calls)
    assert any(any(arg.startswith("--new=4:") for arg in call) for call in sgdisk_calls)
    assert any(call[:2] == ["bash", "-lc"] and "ntfsresize -f -x /dev/sda3" in call[2] for call in calls)


def test_successful_restore_fails_when_partition_layout_is_not_verified(monkeypatch, tmp_path):
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    monkeypatch.setattr(ir, "mount_nfs", lambda *args, **kwargs: (True, "mounted"))
    monkeypatch.setattr(ir, "_NFS_MOUNT_POINT", str(tmp_path))
    monkeypatch.setattr(ir.os.path, "isdir", lambda path: True)
    monkeypatch.setattr(ir, "_ocs_restoredisk", lambda *args, **kwargs: (True, "restore ok"))
    monkeypatch.setattr(ir, "_verify_restore", lambda device, *args, **kwargs: (False, "too much unallocated space"))

    result = ir.run_restore("image", "/dev/sda", "10.255.0.75", "/images/dev")

    assert result["ok"] is False
    assert result["result"] == "FAIL"
    assert "partition layout verification failed" in result["error_message"]
    assert result["firmware_update_trigger"]["installed"] is False


def test_install_post_restore_firmware_trigger_writes_setupcomplete_and_startup(tmp_path, monkeypatch):
    root = tmp_path / "win"
    (root / "Windows" / "Setup" / "Scripts").mkdir(parents=True)
    monkeypatch.setattr(ir, "_WINDOWS_RW_MOUNT_POINT", str(root))
    monkeypatch.setattr(ir, "validate_partition_layout", lambda device: {"os_partition": "/dev/sda3", "issues": []})

    commands = []

    def fake_run(argv, timeout=30):
        commands.append(argv)
        if argv and argv[0] == "mount":
            (root / "Windows").mkdir(exist_ok=True)
        return 0, "", ""

    monkeypatch.setattr(ir, "_run", fake_run)

    result = ir.install_post_restore_firmware_trigger("/dev/sda")

    assert result["ok"] is True
    assert result["installed"] is True
    script = root / "ProgramData" / "VSTL" / "PostRestoreFirmware" / "TriggerFirmwareDriverUpdate.ps1"
    launcher = root / "ProgramData" / "VSTL" / "PostRestoreFirmware" / "RunPostRestoreFirmwareUpdate.cmd"
    setupcomplete = root / "Windows" / "Setup" / "Scripts" / "SetupComplete.cmd"
    startup = (
        root / "ProgramData" / "Microsoft" / "Windows" / "Start Menu"
        / "Programs" / "Startup" / "VSTL-PostRestoreFirmware.cmd"
    )
    assert script.exists()
    assert launcher.exists()
    assert setupcomplete.exists()
    assert startup.exists()
    assert "VSTL_POST_RESTORE_FIRMWARE_TRIGGER" in setupcomplete.read_text(encoding="utf-8")
    assert "TriggerFirmwareDriverUpdate.ps1" in startup.read_text(encoding="utf-8")
    assert ["umount", str(root)] in commands


def test_successful_restore_reports_firmware_trigger(monkeypatch, tmp_path):
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    monkeypatch.setattr(ir, "mount_nfs", lambda *args, **kwargs: (True, "mounted"))
    monkeypatch.setattr(ir, "_NFS_MOUNT_POINT", str(tmp_path))
    monkeypatch.setattr(ir.os.path, "isdir", lambda path: True)
    monkeypatch.setattr(ir, "_ocs_restoredisk", lambda *args, **kwargs: (True, "restore ok"))
    monkeypatch.setattr(ir, "_verify_restore", lambda device, *args, **kwargs: (True, "lsblk ok"))
    monkeypatch.setattr(
        ir,
        "install_post_restore_firmware_trigger",
        lambda device: {"ok": True, "installed": True, "evidence": "trigger installed"},
    )

    result = ir.run_restore("image", "/dev/sda", "10.255.0.75", "/images/dev")

    assert result["ok"] is True
    assert result["firmware_update_trigger"]["installed"] is True
    assert "trigger installed" in result["evidence"]
