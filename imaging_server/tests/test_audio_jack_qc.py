import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QC_PATH = ROOT / "bench-client" / "vstl_qc_tests.py"
TUI_TEXT = (ROOT / "bench-client" / "vstl-imaging-tui.py").read_text(encoding="utf-8")


def load_qc():
    spec = importlib.util.spec_from_file_location("vstl_qc_audio_jack", QC_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


qc = load_qc()


def test_audio_jack_probe_combines_codec_pulse_and_plug_state(monkeypatch):
    monkeypatch.setattr(qc, "_audio_codec_headphone_matches", lambda: ["card0/codec#0 Headphone Pin"])
    monkeypatch.setattr(qc, "_pulse_headphone_port_probe", lambda: (["pactl analog-output-headphones available"], True))
    monkeypatch.setattr(qc, "current_audio_jack_active", lambda: True)
    monkeypatch.setattr(qc, "_run", lambda argv, timeout=5: "Simple mixer control 'Headphone',0")

    probe = qc.probe_audio_jack()

    assert probe["applicable"] is True
    assert probe["plugged"] is True
    assert "Headphone Pin" in probe["evidence"]
    assert "analog-output-headphones" in probe["evidence"]


def test_audio_jack_probe_is_na_only_when_no_pin_port_or_control(monkeypatch):
    monkeypatch.setattr(qc, "_audio_codec_headphone_matches", lambda: [])
    monkeypatch.setattr(qc, "_pulse_headphone_port_probe", lambda: ([], None))
    monkeypatch.setattr(qc, "current_audio_jack_active", lambda: False)
    monkeypatch.setattr(qc, "_run", lambda argv, timeout=5: "")

    probe = qc.probe_audio_jack()

    assert probe["applicable"] is False
    assert probe["evidence"] == "no headphone pin/port/control detected"


def test_audio_jack_prepare_routes_headphones_dynamically(monkeypatch):
    calls = []
    monkeypatch.setattr(
        qc,
        "_alsa_card_indices",
        lambda: [0, 2],
    )
    monkeypatch.setattr(
        qc,
        "_alsa_simple_controls",
        lambda card: ["Master", "Headphone+LO", "Headphone Playback Switch", "Dock Mic"],
    )
    monkeypatch.setattr(
        qc,
        "_alsa_prepare_audio",
        lambda playback, capture: calls.append(("prepare", playback, capture)),
    )
    monkeypatch.setattr(
        qc,
        "_amixer",
        lambda card, args, timeout=2, quiet=True: calls.append(("amixer", card, args)) or "ok",
    )
    monkeypatch.setattr(
        qc,
        "_run",
        lambda argv, timeout=5: calls.append(("run", argv)) or (
            "0\talsa_output.pci-0000_00_1f.3.analog-stereo\tmodule\t...\n"
            if argv[:4] == ["pactl", "list", "short", "sinks"] else ""
        ),
    )

    evidence = qc.prepare_audio_jack_output()

    assert ("prepare", 100, 70) in calls
    assert ("amixer", 0, ["sset", "Auto-Mute Mode", "Disabled"]) in calls
    assert ("amixer", 0, ["sset", "Headphone+LO", "100%", "unmute"]) in calls
    assert ("amixer", 2, ["sset", "Headphone Playback Switch", "100%", "unmute"]) in calls
    assert ("run", ["pactl", "set-sink-port", "@DEFAULT_SINK@", "analog-output-headphones"]) in calls
    assert ("run", ["pactl", "set-sink-port", "alsa_output.pci-0000_00_1f.3.analog-stereo", "analog-output-headphones"]) in calls
    assert "Headphone+LO" in evidence


def test_audio_jack_sample_uses_front_left_right_labels_like_speaker_test(monkeypatch):
    calls = []
    monkeypatch.setattr(qc, "ensure_audio_ready", lambda timeout_sec=4: calls.append(("ready", timeout_sec)) or "ready")
    monkeypatch.setattr(qc, "prepare_audio_jack_output", lambda: calls.append("route") or "routed")
    monkeypatch.setattr(qc, "_headphone_playback_device_args", lambda: [["-D", "plughw:CARD=PCH,DEV=0"]])
    monkeypatch.setattr(
        qc,
        "_write_amplified_spoken_speaker_labels",
        lambda path: calls.append(("write-labels", path)) or True,
    )
    monkeypatch.setattr(
        qc,
        "_play_audio_file",
        lambda path, timeout=8, device_args_list=None: calls.append(
            ("play-file", path, timeout, device_args_list)
        ) or True,
    )
    monkeypatch.setattr(
        qc,
        "_play_generated_sine_test",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("sine fallback must not run when spoken labels play")
        ),
    )

    assert qc.play_audio_jack_sample() is True
    assert calls == [
        ("ready", 4),
        "route",
        ("write-labels", "/tmp/vstl_audio_jack_test.wav"),
        ("play-file", "/tmp/vstl_audio_jack_test.wav", 10, [["-D", "plughw:CARD=PCH,DEV=0"]]),
    ]


def test_audio_jack_sample_does_not_fall_back_to_sine_click(monkeypatch):
    calls = []
    monkeypatch.setattr(qc, "ensure_audio_ready", lambda timeout_sec=4: calls.append(("ready", timeout_sec)) or "ready")
    monkeypatch.setattr(qc, "prepare_audio_jack_output", lambda: calls.append("route") or "routed")
    monkeypatch.setattr(qc, "_headphone_playback_device_args", lambda: [["-D", "plughw:CARD=PCH,DEV=0"]])
    monkeypatch.setattr(qc, "_write_amplified_spoken_speaker_labels", lambda path: False)
    monkeypatch.setattr(qc, "_play_spoken_channel_test", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        qc,
        "_play_generated_sine_test",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("audio-jack must not fall back to sine/click playback")
        ),
    )

    assert qc.play_audio_jack_sample() is False


def test_headphone_playback_prefers_analog_hardware_before_default(monkeypatch):
    monkeypatch.setattr(
        qc,
        "_list_alsa_pcm_devices",
        lambda kind: [
            {
                "card": 0,
                "device": 3,
                "card_id": "HDMI",
                "card_name": "HDA Intel HDMI",
                "device_name": "HDMI 0",
                "label": "HDA Intel HDMI HDMI 0",
                "alsa": "plughw:0,3",
            },
            {
                "card": 1,
                "device": 0,
                "card_id": "PCH",
                "card_name": "HDA Intel PCH",
                "device_name": "ALC Analog",
                "label": "HDA Intel PCH ALC Analog",
                "alsa": "plughw:1,0",
            },
        ],
    )

    args = qc._headphone_playback_device_args()

    assert args[0] == ["-D", "plughw:CARD=PCH,DEV=0"]
    assert ["-D", "default"] not in args
    assert ["-D", "pipewire"] not in args
    assert ["-D", "pulse"] not in args
    assert not any("HDMI" in " ".join(arg) for arg in args)


def test_bundled_front_left_right_samples_are_in_bench_payload():
    sounds = ROOT / "bench-client" / "sounds" / "alsa"
    assert (sounds / "Front_Left.wav").stat().st_size > 1000
    assert (sounds / "Front_Right.wav").stat().st_size > 1000
    active = QC_PATH.read_text(encoding="utf-8")
    assert 'os.path.dirname(__file__), "sounds", "alsa"' in active


def test_generated_sine_command_uses_speaker_test_internal_tone(monkeypatch):
    calls = []

    class Completed:
        returncode = 0

    def fake_run(cmd, capture_output=True, text=True, timeout=12, check=False):
        calls.append(cmd)
        return Completed()

    monkeypatch.setattr(qc.subprocess, "run", fake_run)

    assert qc._play_generated_sine_test(device_args=["-D", "default"]) is True
    assert calls == [[
        "speaker-test", "-D", "default", "-c", "2", "-t", "sine", "-f", "440", "-l", "3",
    ]]


def test_audio_jack_is_dedicated_after_microphone_and_routes_failures():
    assert "audio_jack" in qc.TEST_ORDER
    assert qc.TEST_ORDER.index("audio_jack") == qc.TEST_ORDER.index("microphone") + 1
    assert qc.TEST_LABELS["audio_jack"] == "Audio Jack"
    assert '"audio_jack":  probe_audio_jack' in QC_PATH.read_text(encoding="utf-8")
    assert '"audio_jack":    screen_qc_audio_jack' in TUI_TEXT
    assert "Front Left / Front Right labels" in TUI_TEXT
    assert 'if slot.get("kind") != "audio"' in TUI_TEXT

    result = qc.make_result(
        "audio_jack", applicable=True, ran=True, result="FAIL", remarks="no headphone output"
    )
    summary = qc.summarize([result])
    assert summary["failed"] == ["audio_jack"]
    assert summary["all_passed"] is False


def test_audio_jack_screen_advances_to_standard_verdict_without_retest_key_conflict():
    start = TUI_TEXT.index("def screen_qc_audio_jack(")
    end = TUI_TEXT.index("def _obsolete_screen_qc_touchscreen", start)
    block = TUI_TEXT[start:end]

    assert "Press ENTER to play Front Left / Front Right labels." in block
    assert "After playback, the PASS / FAIL prompt will appear automatically." in block
    assert "ENTER play headphone labels" in block
    assert "front_left_right_playback" in block
    assert "ord(\"c\")" not in block
    assert "ord(\"p\")" not in block
    assert "ord(\"r\")" not in block
    assert "Press R to refresh" not in block
