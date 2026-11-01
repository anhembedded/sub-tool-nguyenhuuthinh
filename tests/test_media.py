"""Regression cases from the batch: visible ink, tail gaps and episode seam."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from hongguo_lite.media import branding_layout, geometry_box, layout_text, offset_cues, tail_alignment, source_audio_scope, _srt_time

ROOT = Path(__file__).resolve().parents[1]
PRESET = json.loads((ROOT / "config/preset.json").read_text(encoding="utf-8"))


def font_file():
    candidates = [os.environ.get("HONGGUO_TEST_FONT", ""), str(ROOT / "assets/font.ttf"), "C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    return next((Path(path) for path in candidates if path and Path(path).is_file()), None)


class MediaRegressionTests(unittest.TestCase):
    def test_visible_glyph_block_is_centered_for_descenders_and_two_lines(self):
        font = font_file()
        if font is None:
            self.skipTest("Set HONGGUO_TEST_FONT to an installed TrueType font")
        for text, box, minimum_lines in [("gyp!", (700, 110), 1), ("Keep every meaning when this long dialogue wraps onto a second line.", (850, 120), 2)]:
            with self.subTest(text=text):
                image, row = layout_text(text, box, font, PRESET["text"])
                self.assertGreaterEqual(len(row["lines"]), minimum_lines)
                self.assertEqual(" ".join(row["lines"]), text)
                bbox = image.getchannel("A").getbbox()
                self.assertLessEqual(abs((bbox[0] + bbox[2]) / 2 - box[0] / 2), .5)
                self.assertLessEqual(abs((bbox[1] + bbox[3]) / 2 - box[1] / 2), .5)
                self.assertEqual(image.getpixel((0, 0))[3], 0)
                colors = {color: count for count, color in image.getcolors(image.width * image.height)}
                self.assertIn(tuple(PRESET["text"]["fill_rgba"]), colors)
                self.assertIn(tuple(PRESET["text"]["stroke_rgba"]), colors)
                yellow_count = colors[tuple(PRESET["text"]["fill_rgba"])]
                self.assertGreater(yellow_count, 10)
                self.assertLess(yellow_count, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) * .8)

    def test_extreme_text_blocks_instead_of_clipping_or_rewriting(self):
        font = font_file()
        if font is None:
            self.skipTest("Set HONGGUO_TEST_FONT")
        with self.assertRaisesRegex(ValueError, "does not fit"):
            layout_text("Unbreakable" * 60, (400, 80), font, PRESET["text"])

    def test_branding_is_fifty_percent_preset_once_and_ratio_preserving(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            Image.new("RGBA", (1254, 1254), (255, 0, 0, 128)).save(temp / "logo.png")
            Image.new("RGBA", (2172, 724), (0, 255, 0, 128)).save(temp / "word.png")
            preset = copy.deepcopy(PRESET)
            preset["branding"]["asset_sha256"] = {}
            env = {"logo": str(temp / "logo.png"), "wordmark": str(temp / "word.png")}
            layout = branding_layout({"width": 1920, "height": 1080}, preset, env)
            self.assertEqual([layout["logo"][key] for key in ("x", "y", "width", "height")], [24, 24, 162, 162])
            self.assertEqual([layout["wordmark"][key] for key in ("x", "y", "width", "height")], [1302, 24, 594, 198])
            portrait = branding_layout({"width": 1080, "height": 1920}, preset, env)
            for row in portrait.values():
                self.assertLessEqual(row["ratio_error_pixels"], .5)
            self.assertEqual(portrait["wordmark"]["x"] + portrait["wordmark"]["width"], 1080 - portrait["wordmark"]["y"])

    def test_measured_source_tail_repair_never_shifts_or_hides_large_loss(self):
        self.assertEqual(tail_alignment(48000, 48000)["silence_appended_samples"], 0)
        for shortfall in (509, 559, 589, 936, 1497, 1504):
            self.assertEqual(tail_alignment(48000 - shortfall, 48000)["silence_appended_samples"], shortfall)
        for original in (48001, 48000 - 1601):
            with self.assertRaises(ValueError):
                tail_alignment(original, 48000)

    def test_roi_cannot_reuse_another_films_geometry(self):
        source = {"width": 1920, "height": 1080, "sha256": "a" * 64}
        geometry = {"source_sha256": "a" * 64, "verified": True, "evidence": [{"frame": 1, "sha256": "b" * 64}], "blur_box": {"x": 64, "y": 890, "width": 1792, "height": 110}}
        self.assertEqual(geometry_box(source, geometry)["y"], 890)
        geometry["source_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "source-hash-specific"):
            geometry_box(source, geometry)

    def test_native_audio_scope_is_explicit_bounded_and_preserves_tail_shortfalls(self):
        selected = source_audio_scope(48000 + 2032, 48000, "native_video")
        self.assertEqual(selected["discarded_OUT_OF_VIDEO_SCOPE_samples"], 2032)
        self.assertEqual(selected["selected_source_samples"], 48000)
        for count, scope in [(48001, None), (48000 + 4097, "native_video")]:
            with self.assertRaisesRegex(ValueError, "extends outside"):
                source_audio_scope(count, 48000, scope)
        short = source_audio_scope(48000 - 589, 48000, "native_video")
        self.assertEqual(short["discarded_OUT_OF_VIDEO_SCOPE_samples"], 0)
        self.assertEqual(short["selected_source_samples"], 48000 - 589)

    def test_second_episode_srt_uses_exact_frame_offset(self):
        def episode(frames, start, end, cue_id):
            return {"source": {"frames": frames, "fps": "30/1"}, "cues": [{"cue_id": cue_id, "start_frame": start, "end_frame_exclusive": end, "canonical_text": "Keep the exact words."}]}
        cues, seams, frames, fps = offset_cues([episode(1801, 1, 30, "a"), episode(903, 3, 50, "b")])
        self.assertEqual(cues[1]["start_frame"], 1804)
        self.assertEqual(cues[1]["source_start_frame"], 3)
        self.assertEqual(seams, [{"frame": 1801, "sample": 2881600, "episode_index": 1}])
        self.assertEqual(_srt_time(cues[1]["start_frame"], fps), "00:01:00,133")
        self.assertEqual(frames, 2704)


if __name__ == "__main__":
    unittest.main()
