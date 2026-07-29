"""
Offline test for the boot-menu patcher in 04_build_live_iso_clonezilla.sh.

Builds a fake Clonezilla extract dir with realistic isolinux.cfg + grub.cfg
samples, runs the SAME Python patcher block (extracted from the bash script),
and asserts the four `ocs_*` parameters land correctly in every Clonezilla
live boot entry — across both BIOS (APPEND) and UEFI (linux) syntaxes.

Run:  cd /app/backend/imaging_server && python3 -m pytest tests/test_menu_patch.py -v
"""
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


# Sample isolinux.cfg fragment lifted from a real Clonezilla 3.2.2 ISO
ISOLINUX_SAMPLE = textwrap.dedent("""\
    DEFAULT vesamenu.c32
    TIMEOUT 300

    LABEL Clonezilla live
      MENU LABEL Clonezilla live (Default settings, VGA 1024x768)
      MENU DEFAULT
      KERNEL /live/vmlinuz
      APPEND initrd=/live/initrd.img boot=live union=overlay username=user config components quiet noswap edd=on nomodeset enforcing=0 locales= keyboard-layouts= ocs_live_run="ocs-live-general" ocs_live_extra_param="" ocs_live_batch="no" vga=791 ip= net.ifnames=0 nosplash

    LABEL Clonezilla live 800x600
      MENU LABEL Clonezilla live (VGA 800x600)
      KERNEL /live/vmlinuz
      APPEND initrd=/live/initrd.img boot=live union=overlay quiet noswap ocs_live_run="ocs-live-general" ocs_live_extra_param="" ocs_live_batch="no" vga=788 nosplash

    LABEL memtest
      MENU LABEL Memory test
      KERNEL /live/memtest
""")

GRUB_SAMPLE = textwrap.dedent("""\
    set timeout=30
    set default=0

    menuentry "Clonezilla live (Default settings, VGA 1024x768)" {
       search --set -f /live/vmlinuz
       linux /live/vmlinuz boot=live union=overlay username=user config components quiet noswap edd=on nomodeset enforcing=0 locales= keyboard-layouts= ocs_live_run="ocs-live-general" ocs_live_extra_param="" ocs_live_batch="no" vga=791 ip= net.ifnames=0 nosplash
       initrd /live/initrd.img
    }

    menuentry "Clonezilla live (Failsafe)" {
       search --set -f /live/vmlinuz
       linux /live/vmlinuz boot=live union=overlay noapic noapm nodma nomce nolapic nosmp ocs_live_run="ocs-live-general" ocs_live_extra_param="" ocs_live_batch="no" vga=normal nosplash
       initrd /live/initrd.img
    }

    menuentry "Memory test" {
       linux16 /live/memtest
    }
""")


# Same Python helper that lives inside 04_build_live_iso_clonezilla.sh.
# We extract it here so we can unit-test it in isolation.
PATCHER_SCRIPT = textwrap.dedent("""\
    import re, sys, pathlib

    root = pathlib.Path(sys.argv[1])

    cfg_paths = []
    for pattern in ("syslinux/*.cfg", "isolinux/*.cfg",
                    "boot/grub/*.cfg", "boot/grub/**/*.cfg",
                    "EFI/**/*.cfg"):
        cfg_paths.extend(root.glob(pattern))

    PARAMS = [
        ('ocs_live_run',    '"/opt/vstl/vstl-imaging-client.sh"'),
        ('ocs_live_batch',  '"yes"'),
        ('ocs_lang',        '"en_US.UTF-8"'),
        ('ocs_live_keymap', '"NONE"'),
    ]

    LIVE_LINE_RE = re.compile(r'^(\\s*)(append|APPEND|linux|linux16|linuxefi)\\b.*\\bboot=live\\b',
                              re.IGNORECASE)

    def patch_line(line):
        new_line = line
        for key, value in PARAMS:
            pat = re.compile(rf'\\b{re.escape(key)}=("[^"]*"|\\S+)')
            if pat.search(new_line):
                new_line = pat.sub(f'{key}={value}', new_line)
            else:
                new_line = new_line.rstrip('\\n') + f' {key}={value}\\n'
        return new_line

    patched_files = 0
    patched_lines = 0
    matched_lines = 0
    for p in sorted(set(cfg_paths)):
        text = p.read_text(errors='replace')
        out, file_hits = [], 0
        for ln in text.splitlines(keepends=True):
            if LIVE_LINE_RE.match(ln):
                matched_lines += 1
                new_ln = patch_line(ln)
                if new_ln != ln:
                    file_hits += 1
                out.append(new_ln)
            else:
                out.append(ln)
        if file_hits:
            p.write_text(''.join(out))
            patched_files += 1
            patched_lines += file_hits

    if matched_lines == 0:
        sys.exit(1)
    print(f"OK lines={patched_lines}/{matched_lines} files={patched_files}")
""")


def _run_patcher(extract_dir: Path) -> str:
    """Run the patcher script against a fake extract dir and return stdout."""
    result = subprocess.run(
        [sys.executable, "-c", PATCHER_SCRIPT, str(extract_dir)],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _setup_fake_extract(tmpdir: Path,
                        isolinux: str = ISOLINUX_SAMPLE,
                        grub: str = GRUB_SAMPLE) -> Path:
    extract = tmpdir / "extract"
    (extract / "syslinux").mkdir(parents=True)
    (extract / "boot" / "grub").mkdir(parents=True)
    (extract / "syslinux" / "isolinux.cfg").write_text(isolinux)
    (extract / "boot" / "grub" / "grub.cfg").write_text(grub)
    return extract


def test_patches_isolinux_default_entry():
    with tempfile.TemporaryDirectory() as td:
        ex = _setup_fake_extract(Path(td))
        _run_patcher(ex)
        out = (ex / "syslinux" / "isolinux.cfg").read_text()

        # ocs_live_run replaced
        assert 'ocs_live_run="/opt/vstl/vstl-imaging-client.sh"' in out
        assert 'ocs_live_run="ocs-live-general"' not in out

        # ocs_live_batch flipped to yes
        assert 'ocs_live_batch="yes"' in out
        assert 'ocs_live_batch="no"' not in out

        # ocs_lang + ocs_live_keymap injected (were absent in template)
        assert 'ocs_lang="en_US.UTF-8"' in out
        assert 'ocs_live_keymap="NONE"' in out


def test_patches_grub_uefi_entry():
    with tempfile.TemporaryDirectory() as td:
        ex = _setup_fake_extract(Path(td))
        _run_patcher(ex)
        out = (ex / "boot" / "grub" / "grub.cfg").read_text()

        assert 'ocs_live_run="/opt/vstl/vstl-imaging-client.sh"' in out
        assert 'ocs_live_run="ocs-live-general"' not in out
        assert 'ocs_live_batch="yes"' in out
        assert 'ocs_lang="en_US.UTF-8"' in out
        assert 'ocs_live_keymap="NONE"' in out


def test_patches_every_live_entry_not_just_default():
    """Both the default 1024x768 entry AND the 800x600 fallback should get patched."""
    with tempfile.TemporaryDirectory() as td:
        ex = _setup_fake_extract(Path(td))
        _run_patcher(ex)
        out = (ex / "syslinux" / "isolinux.cfg").read_text()

        # Two APPEND lines with boot=live → both should be patched
        live_lines = [ln for ln in out.splitlines()
                      if 'boot=live' in ln and 'APPEND' in ln.upper()]
        assert len(live_lines) == 2, f"expected 2 patched APPEND lines, got {len(live_lines)}"
        for ln in live_lines:
            assert 'ocs_live_run="/opt/vstl/vstl-imaging-client.sh"' in ln
            assert 'ocs_live_batch="yes"' in ln
            assert 'ocs_lang="en_US.UTF-8"' in ln
            assert 'ocs_live_keymap="NONE"' in ln


def test_does_not_touch_non_live_entries():
    """Memtest entries (no boot=live) must stay byte-identical."""
    with tempfile.TemporaryDirectory() as td:
        ex = _setup_fake_extract(Path(td))
        _run_patcher(ex)
        out = (ex / "syslinux" / "isolinux.cfg").read_text()
        # The memtest LABEL block has no APPEND with boot=live
        assert "LABEL memtest" in out
        # Make sure no spurious ocs_* keys leaked into the memtest block
        memtest_block = out.split("LABEL memtest", 1)[1]
        assert "ocs_live_run" not in memtest_block
        assert "ocs_lang" not in memtest_block


def test_idempotent_when_already_patched():
    """Running the patcher twice must produce the same output as one run."""
    with tempfile.TemporaryDirectory() as td:
        ex = _setup_fake_extract(Path(td))
        _run_patcher(ex)
        first_pass = (ex / "syslinux" / "isolinux.cfg").read_text()
        _run_patcher(ex)
        second_pass = (ex / "syslinux" / "isolinux.cfg").read_text()
        assert first_pass == second_pass

        # And all 4 params still each appear exactly once per live entry
        for live_line in [ln for ln in second_pass.splitlines()
                          if 'boot=live' in ln and 'APPEND' in ln.upper()]:
            assert live_line.count('ocs_live_run=') == 1
            assert live_line.count('ocs_live_batch=') == 1
            assert live_line.count('ocs_lang=') == 1
            assert live_line.count('ocs_live_keymap=') == 1


def test_fails_loud_when_no_live_entries_found():
    """If Clonezilla layout changes and no live entry is found, exit non-zero."""
    with tempfile.TemporaryDirectory() as td:
        # Empty cfg with no boot=live anywhere
        ex = Path(td) / "extract"
        (ex / "syslinux").mkdir(parents=True)
        (ex / "syslinux" / "isolinux.cfg").write_text("DEFAULT something\nLABEL foo\n  KERNEL /vmlinuz\n  APPEND quiet\n")
        result = subprocess.run(
            [sys.executable, "-c", PATCHER_SCRIPT, str(ex)],
            capture_output=True, text=True,
        )
        assert result.returncode == 1, "patcher should fail when no live entries found"


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
