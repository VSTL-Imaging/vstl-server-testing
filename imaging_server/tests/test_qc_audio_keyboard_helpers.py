import importlib.util
import array
import math
import os
import pathlib
import tempfile
import unittest
import wave


ROOT = pathlib.Path(__file__).resolve().parents[1]
TUI_TEXT = (ROOT / "bench-client" / "vstl-imaging-tui.py").read_text(encoding="utf-8")


def _load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


qc = _load_module("vstl_qc_tests", "bench-client/vstl_qc_tests.py")


class QCAudioKeyboardHelperTests(unittest.TestCase):
    def test_parse_amixer_simple_controls(self):
        text = "\n".join([
            "Simple mixer control 'Master',0",
            "Simple mixer control 'Speaker',0",
            "Simple mixer control 'Master',0",
        ])
        self.assertEqual(qc._parse_amixer_scontrols(text), ["Master", "Speaker"])

    def test_capture_source_parser_and_ranking_prefer_internal_microphone(self):
        text = """
  Items: 'Mic' 'Internal Mic' 'Dock Mic' 'Headset Mic'
  Item0: 'Dock Mic'
"""
        self.assertEqual(
            qc._parse_amixer_enum_items(text),
            ["Mic", "Internal Mic", "Dock Mic", "Headset Mic"],
        )
        self.assertLess(
            qc._rank_capture_source("Internal Mic"),
            qc._rank_capture_source("Dock Mic"),
        )

    def test_capture_source_selector_sets_one_internal_path(self):
        calls = []
        original_amixer = qc._amixer

        def fake_amixer(card, args, **kwargs):
            calls.append((card, args))
            if args[:2] == ["sget", "Input Source"]:
                return "Items: 'Mic' 'Internal Mic' 'Dock Mic'\nItem0: 'Dock Mic'"
            return ""

        try:
            qc._amixer = fake_amixer
            selected = qc._alsa_select_internal_capture_source(0, "Input Source")
        finally:
            qc._amixer = original_amixer

        self.assertEqual(selected, "Internal Mic")
        self.assertIn((0, ["sset", "Input Source", "Internal Mic"]), calls)
        self.assertNotIn((0, ["sset", "Input Source", "Dock Mic"]), calls)

    def test_capture_source_profiles_do_not_invent_missing_mixer_controls(self):
        original_amixer = qc._amixer
        original_cards = qc._alsa_card_indices
        try:
            qc._amixer = lambda *args, **kwargs: "amixer: Unable to find simple control"
            qc._alsa_card_indices = lambda: [0]
            self.assertEqual(qc._capture_source_profiles(), [])
        finally:
            qc._amixer = original_amixer
            qc._alsa_card_indices = original_cards

    def test_capture_source_selector_does_not_set_missing_control(self):
        calls = []
        original_amixer = qc._amixer
        try:
            def fake_amixer(card, args, **kwargs):
                calls.append(args)
                return ""

            qc._amixer = fake_amixer
            self.assertEqual(qc._alsa_select_internal_capture_source(0, "Input Source"), "")
        finally:
            qc._amixer = original_amixer
        self.assertEqual(calls, [["sget", "Input Source"]])

    def test_capture_gain_is_loud_enough_without_maxing_legacy_codecs(self):
        self.assertEqual(qc._capture_control_percent("Capture", 100), 80)
        self.assertEqual(qc._capture_control_percent("Internal Mic", 100), 80)
        self.assertEqual(qc._capture_control_percent("Mic Boost", 100), 20)
        self.assertEqual(qc._capture_control_percent("Mic Boost", 35), 0)
        self.assertIsNone(qc._capture_control_percent("Headset Mic", 100))

    def test_sof_dmic_capture_prefers_hardware_dmic_before_sysdefault(self):
        original_list = qc._list_alsa_pcm_devices
        original_arecord_names = qc._list_alsa_capture_names
        try:
            qc._list_alsa_pcm_devices = lambda kind: [{
                "card": 0,
                "device": 6,
                "card_id": "sofhdadsp",
                "card_name": "sof-hda-dsp",
                "device_name": "Digital Microphone",
                "label": "sof-hda-dsp Digital Microphone",
                "alsa": "plughw:0,6",
            }]
            qc._list_alsa_capture_names = lambda: []
            args = qc._capture_device_args()
        finally:
            qc._list_alsa_pcm_devices = original_list
            qc._list_alsa_capture_names = original_arecord_names

        self.assertEqual(args[0], ["-D", "plughw:CARD=sofhdadsp,DEV=6"])
        self.assertEqual(args[1], ["-D", "plughw:0,6"])
        self.assertIn(["-D", "sysdefault:CARD=sofhdadsp"], args)

    def test_record_then_playback_cycles_legacy_hda_sources_before_playback(self):
        calls = []
        applied_sources = []
        original_run = qc.subprocess.run
        original_peak = qc._pcm16_wav_peak
        original_normalize = qc._normalize_pcm16_wav
        original_prepare = qc._alsa_prepare_audio
        original_capture_args = qc._capture_device_args
        original_source_profiles = qc._capture_source_profiles
        original_apply_profile = qc._alsa_apply_capture_source_profile
        original_playback_args = qc._playback_device_args
        original_preferred = qc._PREFERRED_CAPTURE_ARGS
        original_preferred_profile = qc._PREFERRED_CAPTURE_PROFILE
        original_preferred_format = qc._PREFERRED_CAPTURE_FORMAT
        original_attempts = os.environ.get("VSTL_MIC_RECORD_ATTEMPTS")

        class Result:
            returncode = 0

        current_source = {"profile": None}

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return Result()

        def fake_peak(path):
            return 1200 if current_source["profile"] == (0, "Input Source", "Mic") else 0

        try:
            qc.subprocess.run = fake_run
            qc._PREFERRED_CAPTURE_ARGS = None
            qc._PREFERRED_CAPTURE_PROFILE = None
            qc._PREFERRED_CAPTURE_FORMAT = None
            qc._pcm16_wav_peak = fake_peak
            qc._normalize_pcm16_wav = lambda path, boosted_path: path
            qc._alsa_prepare_audio = lambda *args, **kwargs: None
            qc._capture_device_args = lambda: [["-D", "plughw:0,0"]]
            qc._capture_source_profiles = lambda: [
                (0, "Input Source", "Internal Mic"),
                (0, "Input Source", "Mic"),
            ]
            def fake_apply_profile(profile):
                applied_sources.append(profile)
                current_source["profile"] = profile

            qc._alsa_apply_capture_source_profile = fake_apply_profile
            qc._playback_device_args = lambda: [[]]
            os.environ["VSTL_MIC_RECORD_ATTEMPTS"] = "3"
            self.assertTrue(qc.record_then_playback(
                seconds=1,
                sample_path="/tmp/vstl_test_mic_hda_sources.wav",
                prepare=False,
            ))
        finally:
            qc.subprocess.run = original_run
            qc._pcm16_wav_peak = original_peak
            qc._normalize_pcm16_wav = original_normalize
            qc._alsa_prepare_audio = original_prepare
            qc._capture_device_args = original_capture_args
            qc._capture_source_profiles = original_source_profiles
            qc._alsa_apply_capture_source_profile = original_apply_profile
            qc._playback_device_args = original_playback_args
            qc._PREFERRED_CAPTURE_ARGS = original_preferred
            qc._PREFERRED_CAPTURE_PROFILE = original_preferred_profile
            qc._PREFERRED_CAPTURE_FORMAT = original_preferred_format
            if original_attempts is None:
                os.environ.pop("VSTL_MIC_RECORD_ATTEMPTS", None)
            else:
                os.environ["VSTL_MIC_RECORD_ATTEMPTS"] = original_attempts

        self.assertIn((0, "Input Source", "Internal Mic"), applied_sources)
        self.assertIn((0, "Input Source", "Mic"), applied_sources)
        record_calls = [call for call in calls if call and call[0] == "arecord"]
        self.assertGreaterEqual(len(record_calls), 2)
        self.assertEqual(calls[-1][0], "aplay")

    def test_spoken_speaker_test_uses_alsa_wav_labels(self):
        calls = []
        original_run = qc.subprocess.run

        class Result:
            returncode = 0

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return Result()

        try:
            qc.subprocess.run = fake_run
            self.assertTrue(qc._play_spoken_channel_test(channels=4))
        finally:
            qc.subprocess.run = original_run

        argv, kwargs = calls[0]
        self.assertEqual(argv, [
            "speaker-test", "-c", "4", "-t", "wav", "-l", "1",
        ])
        self.assertGreaterEqual(kwargs["timeout"], 12)

    def test_record_then_playback_without_prepare_records_before_playback_setup(self):
        calls = []
        original_run = qc.subprocess.run
        original_peak = qc._pcm16_wav_peak
        original_normalize = qc._normalize_pcm16_wav
        original_prepare = qc._alsa_prepare_audio
        original_capture_args = qc._capture_device_args
        original_source_profiles = qc._capture_source_profiles
        original_playback_args = qc._playback_device_args
        original_preferred = qc._PREFERRED_CAPTURE_ARGS
        original_preferred_profile = qc._PREFERRED_CAPTURE_PROFILE

        class Result:
            returncode = 0

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return Result()

        try:
            qc.subprocess.run = fake_run
            qc._PREFERRED_CAPTURE_ARGS = None
            qc._PREFERRED_CAPTURE_PROFILE = None
            qc._pcm16_wav_peak = lambda path: 1000
            qc._normalize_pcm16_wav = lambda path, boosted_path: path
            qc._alsa_prepare_audio = lambda *args, **kwargs: calls.append(["prepare-playback"])
            qc._capture_device_args = lambda: [["-D", "plughw:0,0"]]
            qc._capture_source_profiles = lambda: []
            qc._playback_device_args = lambda: [[]]
            self.assertTrue(qc.record_then_playback(
                seconds=3,
                sample_path="/tmp/vstl_test_mic_unit.wav",
                prepare=False,
                status_callback=lambda status: calls.append(["status", status]),
            ))
        finally:
            qc.subprocess.run = original_run
            qc._pcm16_wav_peak = original_peak
            qc._normalize_pcm16_wav = original_normalize
            qc._alsa_prepare_audio = original_prepare
            qc._capture_device_args = original_capture_args
            qc._capture_source_profiles = original_source_profiles
            qc._playback_device_args = original_playback_args
            qc._PREFERRED_CAPTURE_ARGS = original_preferred
            qc._PREFERRED_CAPTURE_PROFILE = original_preferred_profile

        self.assertEqual(calls[0], ["status", "recording"])
        self.assertEqual(calls[1][0], "arecord")
        self.assertIn("plughw:0,0", calls[1])
        self.assertIn("-d", calls[1])
        self.assertEqual(calls[1][calls[1].index("-d") + 1], "3")
        self.assertIn(["status", "playback"], calls)
        self.assertIn(["prepare-playback"], calls)
        self.assertEqual(calls[-1][0], "aplay")

    def test_record_then_playback_rejects_silent_first_capture_path(self):
        calls = []
        original_run = qc.subprocess.run
        original_peak = qc._pcm16_wav_peak
        original_normalize = qc._normalize_pcm16_wav
        original_prepare = qc._alsa_prepare_audio
        original_capture_args = qc._capture_device_args
        original_preferred = qc._PREFERRED_CAPTURE_ARGS
        original_preferred_profile = qc._PREFERRED_CAPTURE_PROFILE
        original_preferred_format = qc._PREFERRED_CAPTURE_FORMAT
        original_source_profiles = qc._capture_source_profiles
        original_playback_args = qc._playback_device_args
        original_attempts = os.environ.get("VSTL_MIC_RECORD_ATTEMPTS")

        class Result:
            returncode = 0

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return Result()

        try:
            qc.subprocess.run = fake_run
            qc._PREFERRED_CAPTURE_ARGS = None
            qc._PREFERRED_CAPTURE_PROFILE = None
            qc._PREFERRED_CAPTURE_FORMAT = None
            qc._pcm16_wav_peak = lambda path: 0 if path.endswith(".wav") and "_1.wav" not in path else 1200
            qc._normalize_pcm16_wav = lambda path, boosted_path: path
            qc._alsa_prepare_audio = lambda *args, **kwargs: None
            qc._capture_source_profiles = lambda: []
            qc._playback_device_args = lambda: [[]]
            qc._capture_device_args = lambda: [
                ["-D", "plughw:0,0"],
                ["-D", "plughw:1,0"],
            ]
            os.environ["VSTL_MIC_RECORD_ATTEMPTS"] = "2"
            self.assertTrue(qc.record_then_playback(
                seconds=1,
                sample_path="/tmp/vstl_test_mic_silent_first.wav",
                prepare=False,
            ))
        finally:
            qc.subprocess.run = original_run
            qc._pcm16_wav_peak = original_peak
            qc._normalize_pcm16_wav = original_normalize
            qc._alsa_prepare_audio = original_prepare
            qc._capture_device_args = original_capture_args
            qc._PREFERRED_CAPTURE_ARGS = original_preferred
            qc._PREFERRED_CAPTURE_PROFILE = original_preferred_profile
            qc._PREFERRED_CAPTURE_FORMAT = original_preferred_format
            qc._capture_source_profiles = original_source_profiles
            qc._playback_device_args = original_playback_args
            if original_attempts is None:
                os.environ.pop("VSTL_MIC_RECORD_ATTEMPTS", None)
            else:
                os.environ["VSTL_MIC_RECORD_ATTEMPTS"] = original_attempts

        record_calls = [call for call in calls if call and call[0] == "arecord"]
        self.assertEqual(len(record_calls), 2)
        self.assertIn("plughw:0,0", record_calls[0])
        self.assertIn("plughw:1,0", record_calls[1])
        self.assertIn("_1.wav", calls[-1][-1])

    def test_select_capture_device_args_keeps_hardware_rank_during_silent_probe(self):
        original_run = qc.subprocess.run
        original_peak = qc._pcm16_wav_peak
        original_capture_args = qc._capture_device_args
        original_preferred = qc._PREFERRED_CAPTURE_ARGS
        original_preferred_profile = qc._PREFERRED_CAPTURE_PROFILE
        original_source_profiles = qc._capture_source_profiles
        recorded_paths = {}

        class Result:
            returncode = 0

        def fake_run(argv, **kwargs):
            path = argv[-1]
            recorded_paths[path] = argv[argv.index("-D") + 1]
            return Result()

        try:
            qc.subprocess.run = fake_run
            qc._capture_device_args = lambda: [
                ["-D", "plughw:0,0"],
                ["-D", "plughw:1,0"],
            ]
            qc._pcm16_wav_peak = lambda path: 40 if recorded_paths.get(path) == "plughw:0,0" else 900
            qc._PREFERRED_CAPTURE_ARGS = None
            qc._PREFERRED_CAPTURE_PROFILE = None
            qc._capture_source_profiles = lambda: []
            chosen = qc._select_capture_device_args()
        finally:
            qc.subprocess.run = original_run
            qc._pcm16_wav_peak = original_peak
            qc._capture_device_args = original_capture_args
            qc._PREFERRED_CAPTURE_ARGS = original_preferred
            qc._PREFERRED_CAPTURE_PROFILE = original_preferred_profile
            qc._capture_source_profiles = original_source_profiles

        self.assertEqual(chosen, ["-D", "plughw:0,0"])

    def test_select_capture_device_args_skips_zero_pcm_success(self):
        original_run = qc.subprocess.run
        original_peak = qc._pcm16_wav_peak
        original_capture_args = qc._capture_device_args
        original_preferred = qc._PREFERRED_CAPTURE_ARGS
        original_preferred_profile = qc._PREFERRED_CAPTURE_PROFILE
        original_preferred_format = qc._PREFERRED_CAPTURE_FORMAT
        original_source_profiles = qc._capture_source_profiles
        recorded_paths = {}

        class Result:
            returncode = 0

        def fake_run(argv, **kwargs):
            path = argv[-1]
            recorded_paths[path] = argv[argv.index("-D") + 1]
            return Result()

        try:
            qc.subprocess.run = fake_run
            qc._capture_device_args = lambda: [
                ["-D", "plughw:0,0"],
                ["-D", "plughw:1,0"],
            ]
            qc._capture_source_profiles = lambda: []
            qc._pcm16_wav_peak = lambda path: 0 if recorded_paths.get(path) == "plughw:0,0" else 850
            qc._PREFERRED_CAPTURE_ARGS = None
            qc._PREFERRED_CAPTURE_PROFILE = None
            qc._PREFERRED_CAPTURE_FORMAT = None
            chosen = qc._select_capture_device_args()
        finally:
            qc.subprocess.run = original_run
            qc._pcm16_wav_peak = original_peak
            qc._capture_device_args = original_capture_args
            qc._PREFERRED_CAPTURE_ARGS = original_preferred
            qc._PREFERRED_CAPTURE_PROFILE = original_preferred_profile
            qc._PREFERRED_CAPTURE_FORMAT = original_preferred_format
            qc._capture_source_profiles = original_source_profiles

        self.assertEqual(chosen, ["-D", "plughw:1,0"])

    def test_select_capture_device_args_skips_noisy_static_probe(self):
        original_run = qc.subprocess.run
        original_activity = qc._pcm16_wav_activity
        original_capture_args = qc._capture_device_args
        original_preferred = qc._PREFERRED_CAPTURE_ARGS
        original_preferred_profile = qc._PREFERRED_CAPTURE_PROFILE
        original_preferred_format = qc._PREFERRED_CAPTURE_FORMAT
        original_source_profiles = qc._capture_source_profiles
        recorded_paths = {}

        class Result:
            returncode = 0

        def fake_run(argv, **kwargs):
            path = argv[-1]
            recorded_paths[path] = argv[argv.index("-D") + 1]
            return Result()

        def fake_activity(path):
            if recorded_paths.get(path) == "plughw:0,0":
                return {
                    "peak": 34000.0,
                    "rms": 22000.0,
                    "envelope": 0.10,
                    "score": 400000.0,
                    "clipping": 0.02,
                    "zcr": 0.36,
                }
            return {
                "peak": 500.0,
                "rms": 80.0,
                "envelope": 0.18,
                "score": 1200.0,
                "clipping": 0.0,
                "zcr": 0.08,
            }

        try:
            qc.subprocess.run = fake_run
            qc._pcm16_wav_activity = fake_activity
            qc._capture_device_args = lambda: [
                ["-D", "plughw:0,0"],
                ["-D", "plughw:1,0"],
            ]
            qc._capture_source_profiles = lambda: []
            qc._PREFERRED_CAPTURE_ARGS = None
            qc._PREFERRED_CAPTURE_PROFILE = None
            qc._PREFERRED_CAPTURE_FORMAT = None
            chosen = qc._select_capture_device_args()
        finally:
            qc.subprocess.run = original_run
            qc._pcm16_wav_activity = original_activity
            qc._capture_device_args = original_capture_args
            qc._PREFERRED_CAPTURE_ARGS = original_preferred
            qc._PREFERRED_CAPTURE_PROFILE = original_preferred_profile
            qc._PREFERRED_CAPTURE_FORMAT = original_preferred_format
            qc._capture_source_profiles = original_source_profiles

        self.assertEqual(chosen, ["-D", "plughw:1,0"])

    def test_record_then_playback_uses_stereo_first_for_old_dell_mic_arrays(self):
        calls = []
        original_run = qc.subprocess.run
        original_peak = qc._pcm16_wav_peak
        original_normalize = qc._normalize_pcm16_wav
        original_prepare = qc._alsa_prepare_audio
        original_capture_args = qc._capture_device_args
        original_preferred = qc._PREFERRED_CAPTURE_ARGS
        original_preferred_profile = qc._PREFERRED_CAPTURE_PROFILE
        original_source_profiles = qc._capture_source_profiles
        original_playback_args = qc._playback_device_args
        capture_channels_by_path = {}

        class Result:
            returncode = 0

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv and argv[0] == "arecord":
                path = argv[-1]
                channels = int(argv[argv.index("-c") + 1])
                rate = int(argv[argv.index("-r") + 1])
                capture_channels_by_path[path] = (channels, rate)
            return Result()

        def fake_peak(path):
            channels, rate = capture_channels_by_path.get(path, (1, 48000))
            if channels == 1 and rate == 48000:
                return 20
            if channels == 2 and rate == 48000:
                return 1200
            return 0

        try:
            qc.subprocess.run = fake_run
            qc._PREFERRED_CAPTURE_ARGS = None
            qc._PREFERRED_CAPTURE_PROFILE = None
            qc._pcm16_wav_peak = fake_peak
            qc._normalize_pcm16_wav = lambda path, boosted_path: path
            qc._alsa_prepare_audio = lambda *args, **kwargs: None
            qc._capture_device_args = lambda: [["-D", "plughw:0,0"]]
            qc._capture_source_profiles = lambda: []
            qc._playback_device_args = lambda: [[]]
            self.assertTrue(qc.record_then_playback(
                seconds=1,
                sample_path="/tmp/vstl_test_mic_old_dell.wav",
                prepare=False,
            ))
        finally:
            qc.subprocess.run = original_run
            qc._pcm16_wav_peak = original_peak
            qc._normalize_pcm16_wav = original_normalize
            qc._alsa_prepare_audio = original_prepare
            qc._capture_device_args = original_capture_args
            qc._PREFERRED_CAPTURE_ARGS = original_preferred
            qc._PREFERRED_CAPTURE_PROFILE = original_preferred_profile
            qc._capture_source_profiles = original_source_profiles
            qc._playback_device_args = original_playback_args

        record_calls = [call for call in calls if call and call[0] == "arecord"]
        self.assertGreaterEqual(len(record_calls), 1)
        self.assertEqual(record_calls[0][record_calls[0].index("-c") + 1], "2")
        self.assertEqual(record_calls[0][record_calls[0].index("-r") + 1], "48000")
        self.assertEqual(calls[-1][0], "aplay")

    def test_record_then_playback_accepts_weak_but_real_legacy_mic_signal(self):
        calls = []
        original_run = qc.subprocess.run
        original_peak = qc._pcm16_wav_peak
        original_normalize = qc._normalize_pcm16_wav
        original_prepare = qc._alsa_prepare_audio
        original_capture_args = qc._capture_device_args
        original_preferred = qc._PREFERRED_CAPTURE_ARGS
        original_preferred_profile = qc._PREFERRED_CAPTURE_PROFILE
        original_source_profiles = qc._capture_source_profiles
        original_playback_args = qc._playback_device_args

        class Result:
            returncode = 0

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return Result()

        try:
            qc.subprocess.run = fake_run
            qc._PREFERRED_CAPTURE_ARGS = None
            qc._PREFERRED_CAPTURE_PROFILE = None
            qc._pcm16_wav_peak = lambda path: 48
            qc._normalize_pcm16_wav = lambda path, boosted_path: path
            qc._alsa_prepare_audio = lambda *args, **kwargs: None
            qc._capture_device_args = lambda: [["-D", "plughw:0,0"]]
            qc._capture_source_profiles = lambda: []
            qc._playback_device_args = lambda: [[]]
            self.assertTrue(qc.record_then_playback(
                seconds=1,
                sample_path="/tmp/vstl_test_mic_weak_legacy.wav",
                prepare=False,
            ))
        finally:
            qc.subprocess.run = original_run
            qc._pcm16_wav_peak = original_peak
            qc._normalize_pcm16_wav = original_normalize
            qc._alsa_prepare_audio = original_prepare
            qc._capture_device_args = original_capture_args
            qc._PREFERRED_CAPTURE_ARGS = original_preferred
            qc._PREFERRED_CAPTURE_PROFILE = original_preferred_profile
            qc._capture_source_profiles = original_source_profiles
            qc._playback_device_args = original_playback_args

        self.assertEqual(calls[-1][0], "aplay")

    def test_microphone_voice_detection_requires_rise_above_quiet_baseline(self):
        quiet = {"peak": 300.0, "rms": 45.0, "envelope": 0.08, "score": 450.0}
        same_noise = {"peak": 305.0, "rms": 46.0, "envelope": 0.09, "score": 455.0}
        spoken_voice = {"peak": 760.0, "rms": 92.0, "envelope": 0.34, "score": 1020.0}

        self.assertFalse(qc._microphone_voice_detected(same_noise, quiet))
        self.assertTrue(qc._microphone_voice_detected(spoken_voice, quiet))

    def test_microphone_baseline_flags_old_dell_static_floor(self):
        self.assertTrue(qc._microphone_baseline_too_noisy({
            "peak": 34737.0,
            "rms": 25091.5,
            "envelope": 0.25,
            "score": 376627.0,
            "clipping": 0.01,
            "zcr": 0.31,
        }))
        self.assertFalse(qc._microphone_baseline_too_noisy({
            "peak": 300.0,
            "rms": 45.0,
            "envelope": 0.08,
            "score": 450.0,
            "clipping": 0.0,
            "zcr": 0.10,
        }))

    def test_record_then_playback_skips_playback_when_voice_detector_rejects_sample(self):
        calls = []
        statuses = []
        original_run = qc.subprocess.run
        original_activity = qc._pcm16_wav_activity
        original_normalize = qc._normalize_pcm16_wav
        original_prepare = qc._alsa_prepare_audio
        original_capture_args = qc._capture_device_args
        original_source_profiles = qc._capture_source_profiles
        original_playback_args = qc._playback_device_args
        original_preferred = qc._PREFERRED_CAPTURE_ARGS
        original_preferred_profile = qc._PREFERRED_CAPTURE_PROFILE
        original_baseline = qc._MICROPHONE_ACTIVITY_BASELINE
        original_audible_peak = qc._MICROPHONE_AUDIBLE_PEAK

        class Result:
            returncode = 0

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return Result()

        try:
            qc.subprocess.run = fake_run
            qc._PREFERRED_CAPTURE_ARGS = None
            qc._PREFERRED_CAPTURE_PROFILE = None
            qc._MICROPHONE_ACTIVITY_BASELINE = {
                "peak": 300.0,
                "rms": 45.0,
                "envelope": 0.08,
                "score": 450.0,
            }
            qc._MICROPHONE_AUDIBLE_PEAK = 50
            qc._pcm16_wav_activity = lambda path: {
                "peak": 60.0,
                "rms": 10.0,
                "envelope": 0.02,
                "score": 80.0,
            }
            qc._normalize_pcm16_wav = lambda path, boosted_path: path
            qc._alsa_prepare_audio = lambda *args, **kwargs: calls.append(["prepare-playback"])
            qc._capture_device_args = lambda: [["-D", "plughw:0,0"]]
            qc._capture_source_profiles = lambda: []
            qc._playback_device_args = lambda: [[]]

            self.assertFalse(qc.record_then_playback(
                seconds=1,
                sample_path="/tmp/vstl_test_mic_detector_rejects.wav",
                prepare=False,
                status_callback=statuses.append,
            ))
        finally:
            qc.subprocess.run = original_run
            qc._pcm16_wav_activity = original_activity
            qc._normalize_pcm16_wav = original_normalize
            qc._alsa_prepare_audio = original_prepare
            qc._capture_device_args = original_capture_args
            qc._capture_source_profiles = original_source_profiles
            qc._playback_device_args = original_playback_args
            qc._PREFERRED_CAPTURE_ARGS = original_preferred
            qc._PREFERRED_CAPTURE_PROFILE = original_preferred_profile
            qc._MICROPHONE_ACTIVITY_BASELINE = original_baseline
            qc._MICROPHONE_AUDIBLE_PEAK = original_audible_peak

        self.assertIn("no voice detected", statuses)
        self.assertIn("playback failed", statuses)
        self.assertNotIn("aplay", [call[0] for call in calls if call])
        self.assertIn("voice_detected=no", qc.microphone_activity_evidence())
        self.assertIn("playback=skipped_no_voice", qc.microphone_activity_evidence())

    def test_prime_microphone_capture_warms_multiple_legacy_formats(self):
        calls = []
        original_run = qc.subprocess.run
        original_capture_args = qc._capture_device_args
        original_preferred = qc._PREFERRED_CAPTURE_ARGS

        class Result:
            returncode = 0

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return Result()

        try:
            qc.subprocess.run = fake_run
            qc._PREFERRED_CAPTURE_ARGS = None
            qc._capture_device_args = lambda: [["-D", "plughw:0,0"]]
            qc._prime_microphone_capture("/tmp/vstl_prime_test.wav")
        finally:
            qc.subprocess.run = original_run
            qc._capture_device_args = original_capture_args
            qc._PREFERRED_CAPTURE_ARGS = original_preferred

        formats = [
            (call[call.index("-c") + 1], call[call.index("-r") + 1])
            for call in calls
            if call and call[0] == "arecord"
        ]
        self.assertEqual(
            formats,
            [("2", "48000"), ("1", "48000")],
        )
        self.assertTrue(all("plughw:0,0" in call for call in calls if call and call[0] == "arecord"))
        self.assertTrue(all("-t" in call and call[call.index("-t") + 1] == "wav" for call in calls))

    def test_capture_candidate_detects_legacy_gain_controls(self):
        for control in ("Record Gain", "Input Gain", "ADC", "ADC Boost"):
            self.assertTrue(qc._alsa_control_is_capture_candidate(control))

    def test_microphone_playback_normalizer_boosts_weak_voice_but_not_dc_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "weak_voice.wav"
            constant = pathlib.Path(tmp) / "constant.wav"
            boosted = pathlib.Path(tmp) / "boosted.wav"
            samples = array.array("h", [100, -100, 50, -50])
            with wave.open(str(source), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(44100)
                wav.writeframes(samples.tobytes())
            with wave.open(str(constant), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(44100)
                wav.writeframes(array.array("h", [300] * 100).tobytes())

            self.assertEqual(qc._normalize_pcm16_wav(str(source), str(boosted)), str(boosted))
            self.assertTrue(boosted.exists())
            self.assertGreater(qc._pcm16_wav_peak(str(boosted)), qc._pcm16_wav_peak(str(source)))

            boosted.unlink()
            self.assertEqual(qc._normalize_pcm16_wav(str(constant), str(boosted)), str(constant))
            self.assertFalse(boosted.exists())

    def test_microphone_peak_ignores_silent_dc_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            constant = pathlib.Path(tmp) / "constant.wav"
            speech = pathlib.Path(tmp) / "speech.wav"
            for path, samples in (
                (constant, array.array("h", [300] * 200)),
                (speech, array.array("h", [300, 900, 300, -300] * 50)),
            ):
                with wave.open(str(path), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(48000)
                    wav.writeframes(samples.tobytes())

            self.assertEqual(qc._pcm16_wav_peak(str(constant)), 0)
            self.assertGreater(qc._pcm16_wav_peak(str(speech)), 500)

    def test_microphone_normalizer_discards_noisy_stereo_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "stereo_old_dell.wav"
            normalized = pathlib.Path(tmp) / "normalized.wav"
            rate = 48000
            frames = array.array("h")
            for index in range(rate):
                noise = 2400 if index % 2 else -2400
                envelope = 0.35 + (0.65 * abs(math.sin(2.0 * math.pi * 2.5 * index / rate)))
                voice = int(1100 * envelope * math.sin(2.0 * math.pi * 220.0 * index / rate))
                frames.extend((noise, voice))
            with wave.open(str(source), "wb") as wav:
                wav.setnchannels(2)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(frames.tobytes())

            stats = qc._pcm16_wav_activity(str(source))
            self.assertEqual(int(stats["channel"]), 1)
            self.assertEqual(qc._normalize_pcm16_wav(str(source), str(normalized)), str(normalized))
            with wave.open(str(normalized), "rb") as wav:
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getframerate(), rate)
            output_stats = qc._pcm16_wav_activity(str(normalized))
            self.assertLess(output_stats["zcr"], 0.2)
            self.assertLess(output_stats["clipping"], 0.001)

    def test_capture_devices_rank_internal_mic_before_hdmi(self):
        original_list = qc._list_alsa_pcm_devices
        try:
            qc._list_alsa_pcm_devices = lambda kind: [
                {
                    "card": 1,
                    "device": 0,
                    "card_name": "HDMI",
                    "device_name": "HDMI Capture",
                    "label": "HDMI Display Capture",
                    "alsa": "plughw:1,0",
                },
                {
                    "card": 0,
                    "device": 0,
                    "card_name": "PCH [HDA Intel PCH]",
                    "device_name": "ALC Analog",
                    "label": "Internal Mic Analog",
                    "alsa": "plughw:0,0",
                },
            ]
            args = qc._capture_device_args()
            self.assertEqual(args[0], ["-D", "plughw:CARD=PCH,DEV=0"])
            self.assertLess(args.index(["-D", "plughw:CARD=PCH,DEV=0"]), args.index(["-D", "sysdefault:CARD=PCH"]))
            self.assertLess(args.index(["-D", "plughw:0,0"]), args.index(["-D", "plughw:1,0"]))
            self.assertLess(args.index(["-D", "plughw:0,0"]), args.index(["-D", "default"]))
        finally:
            qc._list_alsa_pcm_devices = original_list

    def test_microphone_screen_shows_recording_and_playback_status(self):
        self.assertIn("QC - Microphone - {label}", TUI_TEXT)
        self.assertIn("RECORDING", TUI_TEXT)
        self.assertIn("PLAYBACK", TUI_TEXT)
        self.assertIn("status_callback=show_status", TUI_TEXT)
        self.assertIn("record_seconds = qc.microphone_record_seconds()", TUI_TEXT)
        self.assertIn("FINAL MIC WARM-UP", TUI_TEXT)
        self.assertIn("GET READY", TUI_TEXT)
        self.assertIn("time.sleep(1.5)", TUI_TEXT)
        self.assertIn("Recording NOW for {record_seconds} seconds", TUI_TEXT)
        self.assertIn("qc.calibrate_microphone_baseline(seconds=1", TUI_TEXT)
        self.assertIn("qc.prepare_microphone_recording()", TUI_TEXT)
        self.assertNotIn("until playback starts", TUI_TEXT[TUI_TEXT.rindex("def screen_qc_microphone("):])

    def test_audio_profile_selects_legacy_hda_for_old_latitude(self):
        original_dmi = qc._audio_dmi_identity
        original_pci = qc._audio_pci_lines
        try:
            qc._audio_dmi_identity = lambda: "Dell Inc. Latitude 5490 0B06"
            qc._audio_pci_lines = lambda: ["00:1f.3 Audio device [8086:9d71]"]
            self.assertEqual(qc._audio_profile_from_hardware(), "legacy_hda")
        finally:
            qc._audio_dmi_identity = original_dmi
            qc._audio_pci_lines = original_pci

    def test_audio_profile_selects_sof_for_newer_dmic_audio(self):
        original_dmi = qc._audio_dmi_identity
        original_pci = qc._audio_pci_lines
        try:
            qc._audio_dmi_identity = lambda: "Dell Inc. Latitude 5530 0B06"
            qc._audio_pci_lines = lambda: ["00:1f.3 Audio device [8086:51c8]"]
            self.assertEqual(qc._audio_profile_from_hardware(), "sof_dmic")
        finally:
            qc._audio_dmi_identity = original_dmi
            qc._audio_pci_lines = original_pci

    def test_microphone_record_window_is_clamped_to_three_to_five_seconds(self):
        original = os.environ.get("VSTL_MIC_RECORD_SECONDS")
        try:
            os.environ.pop("VSTL_MIC_RECORD_SECONDS", None)
            self.assertEqual(qc.microphone_record_seconds(), 4)
            os.environ["VSTL_MIC_RECORD_SECONDS"] = "2"
            self.assertEqual(qc.microphone_record_seconds(), 3)
            os.environ["VSTL_MIC_RECORD_SECONDS"] = "9"
            self.assertEqual(qc.microphone_record_seconds(), 5)
        finally:
            if original is None:
                os.environ.pop("VSTL_MIC_RECORD_SECONDS", None)
            else:
                os.environ["VSTL_MIC_RECORD_SECONDS"] = original

    def test_microphone_probe_uses_wider_first_run_budget(self):
        self.assertIn('VSTL_MIC_PROBE_ATTEMPTS", "24"', pathlib.Path(
            ROOT / "bench-client" / "vstl_qc_tests.py"
        ).read_text(encoding="utf-8"))
        self.assertIn('VSTL_MIC_RECORD_ATTEMPTS", "6"', pathlib.Path(
            ROOT / "bench-client" / "vstl_qc_tests.py"
        ).read_text(encoding="utf-8"))

    def test_speaker_test_uses_maximum_volume_target(self):
        original = os.environ.get("VSTL_SPEAKER_VOLUME")
        try:
            os.environ.pop("VSTL_SPEAKER_VOLUME", None)
            self.assertEqual(qc.speaker_playback_percent(), 250)
            os.environ["VSTL_SPEAKER_VOLUME"] = "125"
            self.assertEqual(qc.speaker_playback_percent(), 125)
            os.environ["VSTL_SPEAKER_VOLUME"] = "500"
            self.assertEqual(qc.speaker_playback_percent(), 300)
        finally:
            if original is None:
                os.environ.pop("VSTL_SPEAKER_VOLUME", None)
            else:
                os.environ["VSTL_SPEAKER_VOLUME"] = original

    def test_speaker_test_uses_software_sample_preamp(self):
        original = os.environ.get("VSTL_SPEAKER_SAMPLE_GAIN")
        original_label = os.environ.get("VSTL_SPEAKER_LABEL_GAIN")
        try:
            os.environ.pop("VSTL_SPEAKER_SAMPLE_GAIN", None)
            os.environ.pop("VSTL_SPEAKER_LABEL_GAIN", None)
            self.assertEqual(qc.speaker_sample_gain(), 4.0)
            os.environ["VSTL_SPEAKER_SAMPLE_GAIN"] = "9"
            self.assertEqual(qc.speaker_sample_gain(), 8.0)
            os.environ["VSTL_SPEAKER_LABEL_GAIN"] = "3"
            self.assertEqual(qc.speaker_sample_gain(), 3.0)
        finally:
            if original is None:
                os.environ.pop("VSTL_SPEAKER_SAMPLE_GAIN", None)
            else:
                os.environ["VSTL_SPEAKER_SAMPLE_GAIN"] = original
            if original_label is None:
                os.environ.pop("VSTL_SPEAKER_LABEL_GAIN", None)
            else:
                os.environ["VSTL_SPEAKER_LABEL_GAIN"] = original_label

    def test_speaker_test_plays_amplified_spoken_labels_only(self):
        calls = []
        original_prepare = qc._alsa_prepare_audio
        original_write = qc._write_amplified_spoken_speaker_labels
        original_play_file = qc._play_audio_file
        original_spoken = qc._play_spoken_channel_test
        try:
            qc._alsa_prepare_audio = lambda *args, **kwargs: calls.append(("prepare", args))
            qc._write_amplified_spoken_speaker_labels = lambda path: calls.append(("write-labels", path)) or True
            qc._play_audio_file = lambda path, timeout=8: calls.append(("play-file", path, timeout)) or True
            qc._play_spoken_channel_test = lambda *args, **kwargs: calls.append(("speaker-test", args, kwargs)) or False

            self.assertTrue(qc.play_test_tone(seconds=2.5))
        finally:
            qc._alsa_prepare_audio = original_prepare
            qc._write_amplified_spoken_speaker_labels = original_write
            qc._play_audio_file = original_play_file
            qc._play_spoken_channel_test = original_spoken

        self.assertEqual(calls[0][0], "prepare")
        self.assertIn(("write-labels", "/tmp/vstl_speaker_test.wav"), calls)
        self.assertTrue(any(call[0] == "play-file" for call in calls))
        self.assertFalse(any(call[0] == "speaker-test" for call in calls))

    def test_speaker_source_has_no_sine_fallback(self):
        source = pathlib.Path(ROOT / "bench-client" / "vstl_qc_tests.py").read_text(encoding="utf-8")
        active = source[source.index("def play_test_tone("):]
        active = active[:active.index("_PREFERRED_CAPTURE_ARGS")]
        self.assertIn("_write_amplified_spoken_speaker_labels", active)
        self.assertNotIn('"sine"', active)

    def test_keyboard_full_coverage_auto_passes(self):
        self.assertIn("keyboard_auto_pass=true", TUI_TEXT)
        self.assertIn("all_keys.issubset(pressed)", TUI_TEXT)
        self.assertIn("QC - Keyboard Result", TUI_TEXT)
        self.assertIn("Retest keyboard", TUI_TEXT)
        self.assertIn("Edit / reselect keyboard options", TUI_TEXT)
        self.assertIn("Ctrl+Backspace = reselect options", TUI_TEXT)
        self.assertIn("_KEYBOARD_RESELECT_SENTINEL", TUI_TEXT)
        self.assertIn("ecodes.KEY_BACKSPACE", TUI_TEXT)
        self.assertIn('"Digits/Symbols"', TUI_TEXT)
        self.assertIn('"Punctuation"', TUI_TEXT)
        self.assertIn("max(3.0, hold_seconds)", TUI_TEXT)

    def test_audio_tests_are_mandatory_for_both_technician_layers(self):
        for layer in ("L1", "L2"):
            tests = qc.tests_for(layer)
            self.assertIn("speaker", tests)
            self.assertIn("microphone", tests)
            self.assertLess(tests.index("speaker"), tests.index("microphone"))

    def test_active_audio_screens_do_not_silently_return_na(self):
        active_speaker = TUI_TEXT[TUI_TEXT.rindex("def screen_qc_speaker("):]
        active_speaker = active_speaker[:active_speaker.index("def screen_qc_microphone(")]
        active_microphone = TUI_TEXT[TUI_TEXT.rindex("def screen_qc_microphone("):]
        active_microphone = active_microphone[:active_microphone.index("def _obsolete_screen_qc_touchscreen(")]
        self.assertNotIn('return qc.na_result("speaker"', active_speaker)
        self.assertNotIn('return qc.na_result("microphone"', active_microphone)
        self.assertIn("qc.ensure_audio_ready()", active_speaker)
        self.assertIn("qc.ensure_audio_ready()", active_microphone)
        self.assertIn("voice_detected=yes", active_microphone)
        self.assertIn("PASS is locked until voice playback succeeds", active_microphone)

    def test_audio_probe_falls_back_to_kernel_card_inventory(self):
        original_pcm = qc._list_alsa_pcm_devices
        original_cards = qc._list_alsa_cards
        original_kernel = qc._alsa_kernel_cards
        original_direction = qc._alsa_has_pcm_direction
        try:
            qc._list_alsa_pcm_devices = lambda kind: []
            qc._list_alsa_cards = lambda kind: []
            qc._alsa_kernel_cards = lambda: ["card0 sof-hda-dsp"]
            qc._alsa_has_pcm_direction = lambda kind: False
            self.assertTrue(qc.probe_speaker()["applicable"])
            self.assertTrue(qc.probe_microphone()["applicable"])
        finally:
            qc._list_alsa_pcm_devices = original_pcm
            qc._list_alsa_cards = original_cards
            qc._alsa_kernel_cards = original_kernel
            qc._alsa_has_pcm_direction = original_direction

    def test_keyboard_policy_hides_numpad_for_known_no_numpad_laptops(self):
        self.assertFalse(qc.keyboard_numpad_policy(
            "LENOVO | ThinkPad T14 Gen 2",
            ["AT Translated Set 2 keyboard"],
            True,
        ))
        self.assertFalse(qc.keyboard_numpad_policy(
            "Dell Inc. | Latitude 5490",
            ["AT Translated Set 2 keyboard"],
            True,
        ))
        self.assertFalse(qc.keyboard_numpad_policy(
            "HP | HP ProBook 640 G2",
            ["AT Translated Set 2 keyboard"],
            True,
        ))

    def test_keyboard_policy_shows_numpad_for_known_full_size_laptops(self):
        self.assertTrue(qc.keyboard_numpad_policy(
            "LENOVO | ThinkPad T15 Gen 2",
            ["AT Translated Set 2 keyboard"],
            True,
        ))
        self.assertTrue(qc.keyboard_numpad_policy(
            "HP | HP ProBook 650 G5",
            ["AT Translated Set 2 keyboard"],
            True,
        ))

    def test_keyboard_policy_shows_numpad_for_external_keypad_caps(self):
        self.assertTrue(qc.keyboard_numpad_policy(
            "",
            ["Dell USB Keyboard"],
            True,
        ))
        self.assertFalse(qc.keyboard_numpad_policy(
            "",
            ["AT Translated Set 2 keyboard"],
            True,
        ))


if __name__ == "__main__":
    unittest.main()
