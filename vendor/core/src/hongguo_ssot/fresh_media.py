"""Fresh v2 native-aspect media. No retained renderer/profile is modified.

All outputs are new run-local files. Receipts preserve commands and failures;
technical QA never implies pronunciation, listening, or semantic approval.
"""
from __future__ import annotations

from fractions import Fraction
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
import wave

from PIL import Image, ImageDraw, ImageFont

from .output_metadata import FINAL_METADATA_OPTIONS, audit_output_metadata
from .storage import atomic_json, file_hash, validate_output_tree


RATE = 48000
TEMPOS = tuple(Fraction(n, 4) for n in range(5, 16))
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _root(path):
    root = Path(path).absolute()
    validate_output_tree(root, [])
    root.mkdir(parents=True, exist_ok=True)
    return root


def _new(path):
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"Existing artifact preserved: {path}; choose a new version/run")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def execute(command, log_dir, *, timeout=1800, cwd=None):
    """Finite subprocess execution; preserve every attempt, including timeout."""
    log_dir = _root(log_dir)
    receipt = log_dir / (f"command-{time.time_ns()}-{uuid.uuid4().hex[:8]}.json")
    command = [str(item) for item in command]
    record = {"argv": command, "cwd": str(cwd) if cwd else None,
              "status": "RUNNING", "timeout_seconds": timeout}
    atomic_json(receipt, record)
    started = time.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, check=False, cwd=cwd)
        record.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
                      status="PASS" if result.returncode == 0 else "FAIL")
    except (OSError, subprocess.TimeoutExpired) as exc:
        def readable(value):
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")
        record.update(returncode=None, status="FAIL", error=f"{type(exc).__name__}: {exc}",
                      stdout=readable(getattr(exc, "stdout", "")),
                      stderr=readable(getattr(exc, "stderr", "")))
        raise RuntimeError(f"Media command failed; preserved receipt {receipt}") from exc
    finally:
        record["wall_seconds"] = time.monotonic() - started
        atomic_json(receipt, record)
    if result.returncode:
        raise RuntimeError(f"Media command exited {result.returncode}; receipt {receipt}")
    return {**record, "receipt": str(receipt)}


def _probe(path, logs, ffprobe="ffprobe", *options):
    result = execute([ffprobe, "-v", "error", *options, "-of", "json", path], logs)
    if result["stderr"].strip():
        raise ValueError(f"ffprobe reported decode errors; receipt {result['receipt']}")
    return json.loads(result["stdout"])


def samples_at_frame(frame, fps="30/1"):
    if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
        raise ValueError("Frame boundaries must be nonnegative integers")
    fps = Fraction(fps)
    if fps <= 0:
        raise ValueError("FPS must be positive")
    # Every boundary is rounded independently on the 48 kHz sample clock.
    return round(Fraction(frame * RATE, 1) / fps)


def pcm_info(path, *, channels=None, samples=None):
    with wave.open(str(path), "rb") as audio:
        info = {"sample_rate": audio.getframerate(), "channels": audio.getnchannels(),
                "sample_width": audio.getsampwidth(), "samples": audio.getnframes(),
                "compression": audio.getcomptype()}
        actual_bytes = 0
        while block := audio.readframes(RATE * 8):
            actual_bytes += len(block)
        if actual_bytes != info["samples"] * info["sample_width"] * info["channels"]:
            raise ValueError(f"Truncated/misaligned PCM payload: {path}")
    if (info["sample_rate"], info["sample_width"], info["compression"]) != (RATE, 2, "NONE"):
        raise ValueError(f"Expected uncompressed 48 kHz PCM16: {path}: {info}")
    if channels is not None and info["channels"] != channels:
        raise ValueError(f"PCM channel mismatch: {path}")
    if samples is not None and info["samples"] != samples:
        raise ValueError(f"PCM sample mismatch: {path}: {info['samples']} != {samples}")
    if info["channels"] not in (1, 2):
        raise ValueError("Only mono/stereo PCM is accepted")
    return info


def _cue_id(cue):
    result = cue.get("cue_id", cue.get("id"))
    if not isinstance(result, str) or not result:
        raise ValueError("Every cue requires a stable source cue_id")
    return result


def validate_cues(cues, total_frames):
    if not isinstance(total_frames, int) or total_frames <= 0 or not cues:
        raise ValueError("A positive timeline and nonempty cues are required")
    ids = set()
    for index, cue in enumerate(cues):
        key = _cue_id(cue)
        if key in ids:
            raise ValueError("Duplicate cue ID")
        ids.add(key)
        start, end = cue["start_frame"], cue["end_frame_exclusive"]
        if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int)
                or not isinstance(end, int) or not 0 <= start < end <= total_frames):
            raise ValueError(f"Invalid immutable source frame boundaries: {key}")
        if index and cues[index - 1]["end_frame_exclusive"] > start:
            raise ValueError("Source subtitle/timeline overlap cannot be exported")
        text = cue.get("canonical_text")
        if not isinstance(text, str) or not text or text != text.strip():
            raise ValueError("canonical_text must be nonblank, exact, and unpadded")
        if any(char in text for char in "\\{}<>\r\n") or any(ord(char) < 32 for char in text):
            raise ValueError("Canonical subtitle text must not contain technical markup/control data")
        if cue.get("source_cue_ids", [key]) != [key]:
            raise ValueError("A voice event must retain exactly one source cue")


def tempo_filter(tempo):
    remaining = Fraction(tempo)
    if remaining not in TEMPOS:
        raise ValueError("Tempo must use a quarter step between 1.25 and 3.75")
    steps = []
    while remaining > 2:
        steps.append("atempo=2")
        remaining /= 2
    steps.append(f"atempo={float(remaining):.8g}")
    return ",".join(steps)


def fit_cues(cues, raw_audio, run_dir, *, fps="30/1", total_frames, ffmpeg="ffmpeg"):
    """Try actual PCM at 1.25 first, then quarter steps; never trim speech.

    Inputs must already be complete native-speed 48 kHz PCM16, one per cue.
    An overflowing cue preserves every attempt and creates no voice timeline.
    """
    validate_cues(cues, total_frames)
    root = _root(run_dir)
    report_path = _new(root / "fit_report.json")
    report = {"schema_version": 2, "status": "PASS", "fps": str(Fraction(fps)),
              "total_frames": total_frames, "total_samples": samples_at_frame(total_frames, fps),
              "cues": [], "review": {"pronunciation": "NEEDS_REVIEW", "listening": "NEEDS_REVIEW"}}
    for index, cue in enumerate(cues):
        key = _cue_id(cue)
        raw = Path(raw_audio[key]).resolve(strict=True)
        original = pcm_info(raw, channels=1)
        boundary = cues[index + 1]["start_frame"] if index + 1 < len(cues) else total_frames
        start_sample = samples_at_frame(cue["start_frame"], fps)
        window = samples_at_frame(boundary, fps) - start_sample
        row = {"cue_id": key, "canonical_text": cue["canonical_text"], "start_frame": cue["start_frame"],
               "end_frame_exclusive": cue["end_frame_exclusive"], "start_sample": start_sample,
               "available_samples": window, "raw_audio": str(raw), "raw_sha256": file_hash(raw),
               "raw_samples": original["samples"], "tempo_attempts": [], "status": "NEEDS_REVIEW"}
        for tempo in TEMPOS:
            output = _new(root / "clips" / f"cue-{index:06d}-tempo-{int(tempo * 100):03d}.wav")
            execute([ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-n", "-i", raw,
                     "-vn", "-af", tempo_filter(tempo), "-c:a", "pcm_s16le", "-ar", str(RATE),
                     "-ac", "1", output], root / "logs")
            actual = pcm_info(output, channels=1)["samples"]
            attempt = {"tempo": float(tempo), "samples": actual, "available_samples": window,
                       "path": str(output), "sha256": file_hash(output), "fits": actual <= window}
            row["tempo_attempts"].append(attempt)
            if actual <= window:
                row.update(status="PASS", tempo=float(tempo), samples=actual, path=str(output),
                           sha256=attempt["sha256"], end_sample=start_sample + actual)
                break
        if row["status"] != "PASS":
            report["status"] = "NEEDS_REVIEW"
        report["cues"].append(row)
        atomic_json(report_path, report)
    if report["status"] == "PASS":
        timeline = _new(root / "english_timeline.wav")
        cursor = 0
        with wave.open(str(timeline), "wb") as writer:
            writer.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            for row in report["cues"]:
                gap = row["start_sample"] - cursor
                if gap < 0:
                    raise ValueError("Unrepresentable speech overlap; export is forbidden")
                while gap:
                    length = min(gap, RATE)
                    writer.writeframesraw(b"\0" * (2 * length))
                    gap -= length
                with wave.open(row["path"], "rb") as clip:
                    while block := clip.readframes(RATE):
                        writer.writeframesraw(block)
                cursor = row["end_sample"]
            gap = report["total_samples"] - cursor
            while gap:
                length = min(gap, RATE)
                writer.writeframesraw(b"\0" * (2 * length))
                gap -= length
        pcm_info(timeline, channels=1, samples=report["total_samples"])
        report.update(timeline_wav=str(timeline), timeline_sha256=file_hash(timeline))
    atomic_json(report_path, report)
    return report


def evaluate_gates(gates, *, force_export=False, reason=None, trigger=None):
    """A soft override records the original FAIL; hard failures never export."""
    failed = [dict(row) for row in gates if row.get("status") != "PASS"]
    hard = [row for row in failed if row.get("severity") != "soft"]
    override = bool(failed and not hard and force_export and reason and str(reason).strip() and trigger)
    return {"allowed": not failed or override, "status": "BLOCKED" if hard or (failed and not override)
            else ("DRAFT_OVERRIDE" if override else "DRAFT"), "gates": [dict(row) for row in gates],
            "override": {"reason": reason, "trigger": trigger, "gates": [row["gate"] for row in failed]}
            if override else None}


def _rect(value):
    if isinstance(value, dict):
        return tuple(value[key] for key in ("x", "y", "width", "height"))
    return tuple(value)


def _intersects(a, b):
    x, y, w, h = a
    xx, yy, ww, hh = b
    return x < xx + ww and xx < x + w and y < yy + hh and yy < y + h


def validate_layout(profile, width=None, height=None):
    width, height = width or profile["width"], height or profile["height"]
    if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0
            or width % 2 or height % 2):
        raise ValueError("Source size must support unchanged yuv420p resolution")
    blur, sub, brand = profile["blur"], profile["subtitles"], profile["branding"]
    if type(sub.get("cn_reference_glyph_height")) is not int or sub["cn_reference_glyph_height"] < 6:
        raise ValueError("A measured native Chinese reference glyph height is required")
    if type(sub.get("max_lines", 3)) is not int or sub.get("max_lines", 3) < 1:
        raise ValueError("Subtitle max_lines must be a positive integer")
    Path(sub["font_path"]).resolve(strict=True)
    if type(blur.get("radius", 12)) is not int or blur.get("radius", 12) < 1:
        raise ValueError("Blur radius must be a positive integer")
    if (blur.get("verified") is not True or blur.get("film_id") != profile.get("film_id")
            or not _SHA.fullmatch(blur.get("evidence_sha256", ""))):
        raise ValueError("A verified, film-specific fixed blur ROI with evidence hash is required")
    for label, box, margin in [("blur", _rect(blur), 0), ("subtitles", _rect(sub), sub.get("safe_margin", 0))]:
        x, y, w, h = box
        if (type(margin) is not int or margin < 0
                or any(isinstance(n, bool) or not isinstance(n, int) for n in box)
                or not (w > 0 and h > 0 and margin <= x and margin <= y
                        and x + w <= width - margin and y + h <= height - margin)):
            raise ValueError(f"Invalid {label} safety rectangle")
    if blur["width"] * blur["height"] >= width * height * 0.5:
        raise ValueError("Blur must be limited to the verified subtitle ROI")
    if any(value % 2 for value in _rect(blur)):
        raise ValueError("Verified blur ROI must use the even yuv420p chroma grid; implicit coordinate rounding is forbidden")
    if sub["x"] % 2 or sub["y"] % 2:
        raise ValueError("Subtitle position must use the even yuv420p chroma grid")
    regions = [_rect(sub), *[_rect(row) for row in brand.get("critical_regions", [])]]
    for x, y, w, h in regions[1:]:
        if (any(type(n) is not int for n in (x, y, w, h)) or x < 0 or y < 0 or w <= 0 or h <= 0
                or x + w > width or y + h > height):
            raise ValueError("Invalid native critical-detail rectangle")
    occupied = []
    for label in ("logo", "wordmark"):
        asset = brand[label]
        box = _rect(asset)
        x, y, w, h = box
        margin = brand.get("safe_margin", 0)
        if (type(margin) is not int or margin < 0
                or any(isinstance(n, bool) or not isinstance(n, int) for n in box)
                or not (w > 0 and h > 0 and margin <= x and margin <= y
                        and x + w <= width - margin and y + h <= height - margin)):
            raise ValueError(f"Branding outside safe margins: {label}")
        if any(_intersects(box, region) for region in regions + occupied):
            raise ValueError(f"Branding obscures subtitles, critical region or other branding: {label}")
        if x % 2 or y % 2:
            raise ValueError("Brand position must use the even yuv420p chroma grid")
        path = Path(asset["path"]).resolve(strict=True)
        if not _SHA.fullmatch(asset.get("sha256", "")) or file_hash(path) != asset["sha256"]:
            raise ValueError(f"Original user asset hash mismatch: {label}")
        with Image.open(path) as image:
            if not image.width or not image.height:
                raise ValueError("Empty branding asset")
        occupied.append(box)
    return {"status": "PASS", "canvas": [width, height], "blur": _rect(blur),
            "branding": occupied, "critical_regions": regions[1:]}


def raster_subtitles(cues, profile, run_dir):
    """Raster exact canonical text, wrap without editing it, test actual pixels."""
    root = _root(run_dir)
    cfg = profile["subtitles"]
    font_path = Path(cfg["font_path"]).resolve(strict=True)
    target = cfg["cn_reference_glyph_height"] * 2 / 3
    if target < 4:
        raise ValueError("Native Chinese glyph reference is too small")
    choices = []
    for size in range(4, max(12, int(target * 3))):
        font = ImageFont.truetype(str(font_path), size)
        box = font.getbbox("Hgj")
        choices.append((abs(box[3] - box[1] - target), size, box[3] - box[1]))
    _, size, glyph_height = min(choices)
    font = ImageFont.truetype(str(font_path), size)
    rows = []
    width, height = cfg["width"], cfg["height"]
    padding = 4
    for index, cue in enumerate(cues):
        text = cue["canonical_text"]
        # Break at spaces only and preserve all canonical code points in receipt.
        lines, line = [], ""
        for word in text.split(" "):
            candidate = line + (" " if line else "") + word
            if font.getlength(candidate) > width - 2 * padding:
                if not line or font.getlength(word) > width - 2 * padding:
                    raise ValueError(f"Unwrappable subtitle: {_cue_id(cue)}")
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
        if len(lines) > cfg.get("max_lines", 3) or " ".join(lines) != text:
            raise ValueError("Subtitle cannot wrap safely without altering canonical text")
        image = Image.new("RGBA", (width, height))
        draw = ImageDraw.Draw(image)
        spacing = max(2, int(size * 0.12))
        joined = "\n".join(lines)
        bbox = draw.multiline_textbbox((0, 0), joined, font=font, spacing=spacing, stroke_width=2)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if text_w > width - 2 * padding or text_h > height - 2 * padding:
            raise ValueError(f"Raster subtitle exceeds safe box: {_cue_id(cue)}")
        draw.multiline_text(((width - text_w) / 2 - bbox[0], (height - text_h) / 2 - bbox[1]),
                            joined, font=font, fill="white", stroke_width=2, stroke_fill="black",
                            spacing=spacing, align="center")
        actual = image.getchannel("A").getbbox()
        if not actual or not (padding <= actual[0] and padding <= actual[1]
                              and actual[2] <= width - padding and actual[3] <= height - padding):
            raise ValueError("Actual subtitle raster touches unsafe border")
        path = _new(root / f"cue-{index:06d}.png")
        image.save(path)
        rows.append({"cue_id": _cue_id(cue), "canonical_text": text, "lines": lines,
                     "path": str(path), "sha256": file_hash(path), "raster_bbox": actual,
                     "font_size": size, "reference_glyph_height": glyph_height,
                     "target_glyph_height": target, "ratio_to_cn": glyph_height / cfg["cn_reference_glyph_height"]})
    report = {"status": "PASS", "font_sha256": file_hash(font_path), "cues": rows}
    atomic_json(_new(root / "raster_report.json"), report)
    return report


def subtitle_track(cues, raster, profile, total_frames, run_dir):
    """A sparse PNG timeline avoids one FFmpeg input/filter per source cue.

    Only event-boundary packets are needed: framesync retains the exact PNG
    until the next boundary. Explicit source-rate image demuxing puts every
    packet on the native frame clock. A final transparent boundary prevents
    the last subtitle from persisting after its source interval.
    """
    root = _root(run_dir)
    blank = _new(root / "transparent.png")
    Image.new("RGBA", (profile["subtitles"]["width"], profile["subtitles"]["height"])).save(blank)
    segments = []
    cursor = 0
    for cue, row in zip(cues, raster["cues"]):
        start, end = cue["start_frame"], cue["end_frame_exclusive"]
        if start > cursor:
            segments.append((blank, start - cursor, cursor))
        segments.append((Path(row["path"]), end - start, start))
        cursor = end
    if cursor < total_frames:
        segments.append((blank, total_frames - cursor, cursor))
    lines = ["ffconcat version 1.0"]
    fps = Fraction(profile["fps"])
    for path, count, start in segments:
        escaped = str(path.resolve()).replace("\\", "/").replace("'", "'\\''")
        lines += [f"file '{escaped}'", f"option framerate {fps.numerator}/{fps.denominator}",
                  f"duration {float(Fraction(count, 1) / fps):.12f}"]
    escaped = str(blank.resolve()).replace("\\", "/").replace("'", "'\\''")
    lines += [f"file '{escaped}'", f"option framerate {fps.numerator}/{fps.denominator}"]
    path = _new(root / "subtitles.ffconcat")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    atomic_json(_new(root / "subtitle_track.json"), {"fps": str(fps), "total_frames": total_frames,
                "segments": [{"path": str(path), "frames": count, "start_frame": start}
                             for path, count, start in segments], "tail_transparent_frame": total_frames})
    return path


def verify_output(output, profile, total_frames, run_dir, *, ffmpeg="ffmpeg", ffprobe="ffprobe",
                  expected_colors=None, expected_sar=None):
    """Whole-file decode, all frame timestamps, exact container sample duration."""
    root = _root(run_dir)
    media = _probe(output, root / "logs", ffprobe, "-show_streams", "-show_format")
    videos = [row for row in media["streams"] if row["codec_type"] == "video"]
    audios = [row for row in media["streams"] if row["codec_type"] == "audio"]
    errors = []
    if len(videos) != 1 or len(audios) != 1:
        raise ValueError("Output must have exactly one video and one audio stream")
    video, audio = videos[0], audios[0]
    fps = Fraction(profile["fps"])
    if (video.get("codec_name"), video.get("width"), video.get("height"), video.get("pix_fmt")) != ("h264", profile["width"], profile["height"], "yuv420p"):
        errors.append("Output codec/native geometry/pixels differ")
    if expected_sar is not None and video.get("sample_aspect_ratio", "1:1") != expected_sar:
        errors.append("Output pixel/display aspect differs from source")
    for key, value in (expected_colors or {}).items():
        if value not in (None, "unknown", "unspecified") and video.get(key) != value:
            errors.append(f"Source color description changed: {key}")
    for field in ("avg_frame_rate", "r_frame_rate"):
        if Fraction(video.get(field, "0/1")) != fps:
            errors.append(f"Output {field} differs from exact source FPS {fps}")
    if (audio.get("codec_name"), audio.get("sample_rate"), audio.get("channels")) != ("aac", "48000", 2):
        errors.append("Output audio is not AAC48k stereo")
    frames = _probe(output, root / "logs", ffprobe, "-select_streams", "v:0", "-show_frames",
                    "-show_entries", "frame=best_effort_timestamp").get("frames", [])
    tb = Fraction(video["time_base"])
    mismatches = [i for i, row in enumerate(frames)
                  if Fraction(row.get("best_effort_timestamp", -1)) * tb != Fraction(i, 1) / fps]
    if len(frames) != total_frames or mismatches:
        errors.append("Output frame count/timestamps differ from unchanged source timeline")
    audio_samples = Fraction(audio.get("duration_ts", 0)) * Fraction(audio["time_base"]) * RATE
    expected_samples = samples_at_frame(total_frames, fps)
    if audio_samples != expected_samples:
        errors.append(f"Audio container duration {audio_samples} differs from {expected_samples} samples")
    for stream in (video, audio):
        if Fraction(stream.get("start_time", "0")) != 0:
            errors.append("Output stream does not start at zero")
    execute([ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-nostdin", "-i", output,
             "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], root / "logs")
    # AAC decoder padding is inspected and reported separately from exact edit-list duration.
    # Raw PCM permits full-season QA beyond RIFF WAV's 4 GiB file limit.
    decoded = _new(root / "decoded_audio.s16le")
    execute([ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-nostdin", "-n", "-i", output,
             "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
             "-f", "s16le", decoded], root / "logs")
    if decoded.stat().st_size % 4:
        errors.append("Decoded PCM has an incomplete stereo sample")
    decoded_samples = decoded.stat().st_size // 4
    if not expected_samples <= decoded_samples < expected_samples + 1024:
        errors.append("Decoded AAC has unexpected sample padding/loss")
    report = {"status": "PASS" if not errors else "FAIL", "errors": errors, "output": str(output),
              "sha256": file_hash(Path(output)), "frames": len(frames), "fps": str(fps),
              "timestamp_mismatches": mismatches[:20], "expected_samples": expected_samples,
              "container_audio_samples": int(audio_samples), "decoded_audio_samples": decoded_samples,
              "decoded_audio_pcm": str(decoded),
              "aac_padding_samples": decoded_samples - expected_samples, "whole_file_decode": "PASS",
              "media": media, "independent_semantic_review": "NEEDS_REVIEW",
              "independent_listening_review": "NEEDS_REVIEW"}
    atomic_json(_new(root / "media_qa.json"), report)
    return report


def render_source_aspect(sources, cues, fitted_audio, background_stems, profile, run_dir, *,
                         ffmpeg="ffmpeg", ffprobe="ffprobe", force_export=False,
                         override_reason=None, review_gates=()):
    """Encode unchanged native frames with fixed blur, English pixels and brand assets.

    ``background_stems`` has one ordered record per source episode, either
    episode/music_effects/music_effects_sha256 or separate music/sfx/hash fields.
    Speech stems are never read. Full episode stems may be end-trimmed to the
    selected video interval; deficient stems are rejected, never padded.
    Separation latency, leakage and lost effects require independent review.
    """
    root = _root(run_dir)
    output = _new(root / "video-v001_DRAFT.mp4")
    partial = _new(root / "video-v001_DRAFT.partial.mp4")
    gates = evaluate_gates(review_gates, force_export=force_export,
                           reason=override_reason, trigger="--force-export" if force_export else None)
    atomic_json(_new(root / "export_gates.json"), gates)
    if not gates["allowed"]:
        raise ValueError("Export gates block render; original failure results are preserved")
    if fitted_audio.get("status") != "PASS":
        raise ValueError("Missing/overflowing cue audio is a hard export failure")
    total_frames = sum(row["frames"] for row in sources)
    validate_cues(cues, total_frames)
    if (fitted_audio["total_frames"] != total_frames
            or Fraction(fitted_audio["fps"]) != Fraction(profile["fps"])):
        raise ValueError("Fitted audio is stale for the source timeline")
    timeline = Path(fitted_audio["timeline_wav"])
    if file_hash(timeline) != fitted_audio["timeline_sha256"]:
        raise ValueError("Voice timeline missing/corrupt")
    pcm_info(timeline, channels=1, samples=samples_at_frame(total_frames, profile["fps"]))
    if len(cues) != len(fitted_audio["cues"]):
        raise ValueError("Missing fitted cue")
    for cue, fitted in zip(cues, fitted_audio["cues"]):
        if any(cue.get(key) != fitted.get(key) for key in ("canonical_text", "start_frame", "end_frame_exclusive")) or _cue_id(cue) != fitted["cue_id"]:
            raise ValueError("Subtitle/TTS text or source lineage changed after fit")
    validate_layout(profile)
    if not sources or len(sources) != len(background_stems):
        raise ValueError("A background music+SFX record is required for every source")
    if len({row["episode"] for row in sources}) != len(sources):
        raise ValueError("Duplicate episode in source scope")
    command = [ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-nostdin", "-n", "-threads", "2"]
    colors, source_sars = [], []
    for index, source in enumerate(sources):
        path = Path(source["path"]).resolve(strict=True)
        if file_hash(path) != source["sha256"]:
            raise ValueError("Source hash mismatch")
        media = _probe(path, root / "logs", ffprobe, "-show_streams", "-count_frames")
        video = [row for row in media["streams"] if row["codec_type"] == "video"][0]
        if ((video["width"], video["height"]) != (profile["width"], profile["height"])
                or Fraction(video["avg_frame_rate"]) != Fraction(profile["fps"])
                or Fraction(video["r_frame_rate"]) != Fraction(profile["fps"])
                or int(video["nb_read_frames"]) != source.get("source_frames", source["frames"])
                or not 0 < source["frames"] <= int(video["nb_read_frames"])):
            raise ValueError("Native source geometry/FPS/frame scope mismatch")
        if video.get("pix_fmt") != "yuv420p" or video.get("color_transfer") in ("smpte2084", "arib-std-b67"):
            raise ValueError("Unreviewed pixel/HDR conversion is forbidden")
        colors.append({key: video.get(key) for key in ("color_range", "color_space", "color_transfer", "color_primaries")})
        source_sars.append(video.get("sample_aspect_ratio", "1:1"))
        command += ["-i", str(path)]
    if any(row != colors[0] for row in colors):
        raise ValueError("Sources with differing color descriptions need explicit review")
    if any(sar != source_sars[0] for sar in source_sars):
        raise ValueError("Sources have differing pixel aspects; native aspect cannot be retained")
    source_count = len(sources)
    command += ["-i", str(timeline)]
    stem_indexes = []
    stem_input_count = 0
    for source, stems in zip(sources, background_stems):
        if source["episode"] != stems["episode"]:
            raise ValueError("Separation stems differ from requested episode order")
        pair = []
        for name in (("music_effects",) if "music_effects" in stems else ("music", "sfx")):
            path = Path(stems[name]).resolve(strict=True)
            if file_hash(path) != stems[name + "_sha256"]:
                raise ValueError("Separated stem hash mismatch")
            info = pcm_info(path, channels=2)
            if info["samples"] < samples_at_frame(source["frames"], profile["fps"]):
                raise ValueError("Separated stem is deficient; padding would hide a timeline failure")
            pair.append(source_count + 1 + stem_input_count)
            stem_input_count += 1
            command += ["-i", str(path)]
        stem_indexes.append(pair)
    raster = raster_subtitles(cues, profile, root / "subtitles")
    overlay_start = source_count + 1 + stem_input_count
    assets = [profile["branding"][key] for key in ("logo", "wordmark")]
    for asset in assets:
        command += ["-i", str(Path(asset["path"]).resolve())]
    subtitle_input = subtitle_track(cues, raster, profile, total_frames, root / "subtitle_track")
    command += ["-f", "concat", "-safe", "0", "-i", str(subtitle_input)]
    filters = []
    for index in range(source_count):
        filters.append(f"[{index}:v]trim=end_frame={sources[index]['frames']},setpts=PTS-STARTPTS[v{index}]")
    if source_count == 1:
        filters.append("[v0]null[base]")
    else:
        filters.append("".join(f"[v{i}]" for i in range(source_count)) + f"concat=n={source_count}:v=1:a=0[base]")
    blur = profile["blur"]
    radius = min(int(blur.get("radius", 12)), max(1, (min(blur["width"], blur["height"]) - 1) // 2))
    chroma_radius = max(0, min(radius, min(blur["width"], blur["height"]) // 4 - 1))
    filters += ["[base]split=2[canvas][roi]",
                f"[roi]crop={blur['width']}:{blur['height']}:{blur['x']}:{blur['y']}:exact=1,boxblur=luma_radius={radius}:luma_power=2:chroma_radius={chroma_radius}:chroma_power=2[blurred]",
                f"[canvas][blurred]overlay={blur['x']}:{blur['y']}:eof_action=pass[blurbase]"]
    prior = "blurbase"
    for index, asset in enumerate(assets):
        label = f"brand{index}"
        filters.append(f"[{overlay_start + index}:v]format=rgba,scale={asset['width']}:{asset['height']}:flags=lanczos[{label}]")
        filters.append(f"[{prior}][{label}]overlay={asset['x']}:{asset['y']}:eof_action=repeat:repeatlast=1[branded{index}]")
        prior = f"branded{index}"
    sub = profile["subtitles"]
    filters.append(f"[{prior}][{overlay_start + 2}:v]overlay={sub['x']}:{sub['y']}:eof_action=repeat:repeatlast=1,trim=end_frame={total_frames},format=yuv420p[vout]")
    for index, pair in enumerate(stem_indexes):
        selected_samples = samples_at_frame(sources[index]["frames"], profile["fps"])
        for number, input_index in enumerate(pair):
            filters.append(f"[{input_index}:a]atrim=end_sample={selected_samples},asetpts=N/SR/TB[stem{index}_{number}]")
        if len(pair) == 1:
            filters.append(f"[stem{index}_0]anull[bg{index}]")
        else:
            filters.append(f"[stem{index}_0][stem{index}_1]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[bg{index}]")
    if source_count == 1:
        filters.append("[bg0]anull[background]")
    else:
        filters.append("".join(f"[bg{i}]" for i in range(source_count)) + f"concat=n={source_count}:v=0:a=1[background]")
    samples = samples_at_frame(total_frames, profile["fps"])
    filters += ["[background]volume=-15dB[bgquiet]",
                f"[{source_count}:a]pan=stereo|c0=c0|c1=c0,volume=0dB[voice]",
                "[bgquiet][voice]amix=inputs=2:duration=first:normalize=0:dropout_transition=0,"
                f"alimiter=limit=0.89:level=0:latency=1,apad=whole_len={samples},atrim=end_sample={samples},asetpts=N/SR/TB[aout]"]
    filter_file = _new(root / "filtergraph.txt")
    filter_file.write_text(";\n".join(filters) + "\n", encoding="utf-8")
    command += ["-filter_complex_threads", "2", "-/filter_complex", str(filter_file),
                "-map", "[vout]", "-map", "[aout]", "-fps_mode", "passthrough",
                "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-maxrate", "6M", "-bufsize", "12M",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                "-movie_timescale", "48000", "-movflags", "+faststart"]
    for field, option in (("color_range", "-color_range"), ("color_space", "-colorspace"),
                          ("color_transfer", "-color_trc"), ("color_primaries", "-color_primaries")):
        if colors[0].get(field) not in (None, "unknown", "unspecified"):
            command += [option, colors[0][field]]
    if colors[0].get("color_range") in ("tv", "pc"):
        # libx264 can omit VUI for its default limited range even when the
        # encoder flag is supplied. Preserve the source declaration explicitly.
        full = 1 if colors[0]["color_range"] == "pc" else 0
        command += ["-bsf:v", f"h264_metadata=video_full_range_flag={full}"]
    command += [*FINAL_METADATA_OPTIONS, str(partial)]
    # Windows limits process argv length. A graph file and relative input names
    # preserve full-season scope without a command argument per subtitle cue.
    candidates = [root, Path(sources[0]["path"]).resolve().parent]
    def local_command(folder):
        result = list(command)
        for index in range(1, len(result)):
            if result[index - 1] == "-i":
                try:
                    result[index] = os.path.relpath(str(result[index]), str(folder))
                except ValueError:  # Different Windows volume: keep absolute.
                    pass
        return result
    working_dir = min(candidates, key=lambda folder: len(subprocess.list2cmdline(local_command(folder))))
    command = local_command(working_dir)
    if os.name == "nt" and len(subprocess.list2cmdline(command)) >= 32000:
        raise ValueError("Source input argv exceeds Windows capacity; split into explicitly scoped outputs without dropping episodes")
    rendered = execute(command, root / "logs", timeout=7200, cwd=working_dir)
    qa = verify_output(partial, profile, total_frames, root / "qa", ffmpeg=ffmpeg, ffprobe=ffprobe,
                       expected_colors=colors[0], expected_sar=source_sars[0])
    if qa["status"] != "PASS":
        raise ValueError("Whole-file native media QA failed; partial and failures preserved")
    metadata = audit_output_metadata(partial, root / "metadata_audit.json", ffprobe=ffprobe,
                                     render_command=command)
    partial.rename(output)
    report = {"status": gates["status"], "output": str(output), "sha256": file_hash(output),
              "total_frames": total_frames, "fps": str(Fraction(profile["fps"])), "profile": profile,
              "source_hashes": [row["sha256"] for row in sources], "source_episodes": [row["episode"] for row in sources],
              "source_colors": colors[0], "source_sample_aspect_ratio": source_sars[0],
              "media_qa": qa, "metadata": metadata, "raster": raster, "render_receipt": rendered["receipt"],
              "gates": gates, "independent_semantic_review": "NEEDS_REVIEW",
              "independent_pronunciation_review": "NEEDS_REVIEW", "independent_listening_review": "NEEDS_REVIEW",
              "limitations": ["Synthetic/media QA does not establish intelligibility, correct pronunciation, semantic accuracy or clean separation.",
                              "AAC decoder padding is reported separately from the exact container timeline."]}
    atomic_json(_new(root / "render_report.json"), report)
    return report
