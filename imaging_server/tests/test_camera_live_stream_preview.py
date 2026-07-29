from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TUI = (ROOT / "bench-client" / "vstl-imaging-tui.py").read_text(encoding="utf-8")


class CameraLiveStreamPreviewTests(unittest.TestCase):
    def test_camera_preview_prefers_ffmpeg_native_fbdev_video(self):
        self.assertIn("def _camera_ffmpeg_fbdev_preview", TUI)
        self.assertIn('"-f", "fbdev", "/dev/fb0"', TUI)
        native_pos = TUI.index("_camera_ffmpeg_fbdev_preview(device, deadline)")
        pipe_pos = TUI.index("_camera_ffmpeg_fb_preview(device, deadline)")
        self.assertLess(native_pos, pipe_pos)

    def test_camera_preview_keeps_raw_framebuffer_compatibility_fallback(self):
        self.assertIn("def _camera_ffmpeg_fb_preview", TUI)
        self.assertIn('"ffmpeg", "-hide_banner", "-loglevel", "error"', TUI)
        self.assertIn('"-pix_fmt", "bgra", "-f", "rawvideo", "pipe:1"', TUI)
        self.assertIn("_fb0_blit_bgra(raw, fb_w, fb_h)", TUI)
        ffmpeg_pos = TUI.index("_camera_ffmpeg_fb_preview(device, deadline)")
        python_yuyv_pos = TUI.index("_camera_fb_v4l2_stream_preview(device, deadline)")
        self.assertLess(ffmpeg_pos, python_yuyv_pos)

    def test_camera_preview_uses_continuous_v4l2_stream_fifo(self):
        self.assertIn("os.mkfifo", TUI)
        self.assertIn("--stream-to={stream_to}", TUI)
        self.assertIn("continuous v4l2 YUYV framebuffer", TUI)
        self.assertIn("_fb0_blit_yuyv(raw, width, height, mirror=True)", TUI)

    def test_camera_preview_is_mirrored_across_renderers(self):
        self.assertIn('f"fps={fps},hflip,"', TUI)
        self.assertIn('"--vf=hflip"', TUI)
        self.assertIn('"-vf", "hflip"', TUI)
        self.assertIn('"--no-banner", "--flip", "h", "--frames", "1"', TUI)

    def test_camera_still_frame_preview_is_only_degraded_fallback(self):
        stream_pos = TUI.index("_camera_fb_v4l2_stream_preview(device, deadline)")
        still_pos = TUI.index("_camera_fb_v4l2_snapshot_preview(device, deadline)")
        self.assertLess(stream_pos, still_pos)
        self.assertIn("camera preview degraded", TUI)


if __name__ == "__main__":
    unittest.main()
