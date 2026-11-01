"""Native media stages. Image/audio/final QA have separate cache boundaries.

No function approves listening or meaning. The controller owns approval and
atomic stage receipts; these functions only write inside their attempt folder.
"""
from __future__ import annotations

from array import array
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFont
import numpy as np

RATE = 48000
ROOT = Path(__file__).resolve().parents[1]
META = ["-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1"]


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _folder(out):
    path = Path(out).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _new(path):
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"Preserve previous artifact; choose a new attempt: {path}")
    return path


def _core():
    src = str(ROOT / "vendor/core/src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from hongguo_ssot import fresh_media, fresh_media_v3
    return fresh_media, fresh_media_v3


def _run(argv, out, *, timeout=7200):
    out = _folder(out)
    stem = f"command-{time.time_ns()}"
    args = list(map(str, argv))
    receipt = {"argv": args, "started_utc": datetime.now(timezone.utc).isoformat(), "status": "RUNNING"}
    save(out / f"{stem}.json", receipt)
    start = time.monotonic()
    try:
        with (out / f"{stem}.stdout.log").open("wb") as stdout, (out / f"{stem}.stderr.log").open("wb") as stderr:
            process = subprocess.run(args, stdout=stdout, stderr=stderr, timeout=timeout, check=False)
        receipt.update(returncode=process.returncode, status="PASS" if process.returncode == 0 else "FAIL")
        if process.returncode:
            raise RuntimeError(f"Media process failed ({process.returncode}); see {out / (stem + '.stderr.log')}")
    except Exception as exc:
        receipt.update(status="FAIL", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        receipt.update(wall_seconds=time.monotonic() - start, ended_utc=datetime.now(timezone.utc).isoformat())
        save(out / f"{stem}.json", receipt)
    return str(out / f"{stem}.json")


def _probe(path, ffprobe):
    proc = subprocess.run([str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-show_chapters", "-of", "json", str(path)], capture_output=True, text=True, encoding="utf-8", timeout=120, check=True)
    if proc.stderr.strip():
        raise ValueError(f"Media probe error: {proc.stderr[:500]}")
    return json.loads(proc.stdout)


def samples_at_frame(frame, fps):
    if type(frame) is not int or frame < 0 or Fraction(fps) <= 0:
        raise ValueError("Invalid exact frame clock")
    return round(Fraction(frame * RATE, 1) / Fraction(fps))


def _video(source, env):
    return source.get("video_stream") or next(row for row in _probe(source["path"], env["ffprobe"])["streams"] if row["codec_type"] == "video")


def _scale(source):
    return min(source["width"] / 1920, source["height"] / 1080)


def _validate_cues(cues, source):
    previous = 0
    ids = set()
    for cue in cues:
        key = cue.get("cue_id", cue.get("id"))
        start, end = cue["start_frame"], cue["end_frame_exclusive"]
        text = cue["canonical_text"]
        if not key or key in ids or type(start) is not int or type(end) is not int or not previous <= start < end <= source["frames"]:
            raise ValueError("Invalid, overlapping, or duplicated cue on source clock")
        if not isinstance(text, str) or not text or text != text.strip() or any(c in text for c in "\r\n\t"):
            raise ValueError("Canonical text must be nonempty single-line text")
        ids.add(key)
        previous = end


def geometry_box(source, geometry):
    """Require source-specific measured evidence; never invent a global ROI."""
    if geometry.get("source_sha256") != source["sha256"] or geometry.get("verified") is not True or not geometry.get("evidence"):
        raise ValueError("Geometry needs verified, source-hash-specific evidence")
    box = geometry["blur_box"]
    x, y, w, h = (box[k] for k in ("x", "y", "width", "height"))
    if any(type(v) is not int or v % 2 for v in (x, y, w, h)) or x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > source["width"] or y + h > source["height"]:
        raise ValueError("Blur box must fit the native even chroma grid")
    if w * h >= source["width"] * source["height"] / 2:
        raise ValueError("Blur box must be limited to caption region")
    return dict(box)


def branding_layout(source, preset, env):
    cfg = preset["branding"]
    scale = min(source["width"] / cfg["reference_width"], source["height"] / cfg["reference_height"])
    margin = round(cfg["margin_at_1080"] * scale)
    result = {}
    for key in ("logo", "wordmark"):
        path = Path(env[key]).resolve(strict=True)
        expected = cfg.get("asset_sha256", {}).get(key)
        digest = sha(path)
        if expected and digest != expected:
            raise ValueError(f"Original {key} asset hash differs")
        with Image.open(path) as asset:
            if "A" not in asset.getbands():
                raise ValueError("Original branding must retain its alpha channel")
            ow, oh = asset.size
        w = max(1, round(cfg[f"{key}_width_at_1080"] * scale))
        h = max(1, round(w * oh / ow))
        x = margin if key == "logo" else source["width"] - margin - w
        if x < 0 or margin + h > source["height"]:
            raise ValueError("Brand asset does not fit native canvas")
        result[key] = {"path": str(path), "sha256": digest, "x": x, "y": margin, "width": w, "height": h, "original_size": [ow, oh], "ratio_error_pixels": abs(h - w * oh / ow)}
    return result


def _wrap(text, font, width, stroke):
    # Every code point survives. Only existing single spaces can become line breaks.
    words = text.split(" ")
    if any(not word for word in words):
        raise ValueError("Canonical text contains repeated spaces; resolve editorially")
    lines, line = [], ""
    for word in words:
        candidate = f"{line} {word}" if line else word
        bbox = font.getbbox(candidate, stroke_width=stroke)
        if bbox[2] - bbox[0] > width:
            if not line:
                return None
            lines.append(line)
            line = word
            bb = font.getbbox(word, stroke_width=stroke)
            if bb[2] - bb[0] > width:
                return None
        else:
            line = candidate
    return lines + [line]


def layout_text(text, box_size, font_path, config, scale=1):
    """Center the actual final alpha ink, including all lines and stroke.

    Pillow font metrics alone do not center visible glyphs (e.g. descenders).
    Crop to the measured alpha bitmap, then position that complete block.
    """
    width, height = box_size
    px = max(1, round(config["padding_x_at_1080"] * scale))
    py = max(1, round(config["padding_y_at_1080"] * scale))
    stroke = max(1, round(config["stroke_width_at_1080"] * scale))
    spacing = max(1, round(config["line_spacing_at_1080"] * scale))
    low = max(4, round(config["minimum_font_size_at_1080"] * scale))
    high = max(low, round(config["font_size_at_1080"] * scale))
    for size in range(high, low - 1, -1):
        font = ImageFont.truetype(str(font_path), size)
        lines = _wrap(text, font, width - 2 * px, stroke)
        if lines is None or len(lines) > config["max_lines"]:
            continue
        if " ".join(lines) != text:
            raise ValueError("Wrapping would edit canonical text")
        joined = "\n".join(lines)
        canvas = Image.new("RGBA", (width * 2, max(height * 3, size * 5)))
        draw = ImageDraw.Draw(canvas)
        bb = draw.multiline_textbbox((0, 0), joined, font=font, spacing=spacing, align="center", stroke_width=stroke)
        draw.multiline_text((stroke + 2 - bb[0], stroke + 2 - bb[1]), joined, font=font, spacing=spacing, align="center", fill=tuple(config["fill_rgba"]), stroke_width=stroke, stroke_fill=tuple(config["stroke_rgba"]))
        ink = canvas.getchannel("A").getbbox()
        if not ink:
            raise ValueError("Text produces no visible glyphs")
        cropped = canvas.crop(ink)
        if cropped.width > width - 2 * px or cropped.height > height - 2 * py:
            continue
        x, y = (width - cropped.width) // 2, (height - cropped.height) // 2
        image = Image.new("RGBA", (width, height))
        image.alpha_composite(cropped, (x, y))
        actual = image.getchannel("A").getbbox()
        if not (px <= actual[0] and py <= actual[1] and actual[2] <= width - px and actual[3] <= height - py):
            raise ValueError("Rendered ink exceeds padding")
        return image, {"canonical_text": text, "lines": lines, "font_size": size, "font_sha256": sha(font_path), "stroke_width": stroke, "line_spacing": spacing, "padding": [px, py], "actual_ink_bbox": list(actual), "center_error_pixels": [(actual[0] + actual[2] - width) / 2, (actual[1] + actual[3] - height) / 2], "fill_rgba": config["fill_rgba"], "stroke_rgba": config["stroke_rgba"], "background": "transparent"}
    raise ValueError("Subtitle does not fit measured box at allowed font sizes; change geometry/style through preview, never edit text silently")


def _prepare(source, cues, geometry, preset, out, env):
    _validate_cues(cues, source)
    box = geometry_box(source, geometry)
    brands = branding_layout(source, preset, env)
    raster = out / "raster"
    raster.mkdir()
    rows = []
    for index, cue in enumerate(cues):
        image, row = layout_text(cue["canonical_text"], (box["width"], box["height"]), env["font"], preset["text"], _scale(source))
        path = raster / f"cue-{index:06d}.png"
        image.save(path)
        rows.append({**row, "cue_id": cue.get("cue_id", cue.get("id")), "start_frame": cue["start_frame"], "end_frame_exclusive": cue["end_frame_exclusive"], "path": str(path), "sha256": sha(path)})
    layout = {"source_sha256": source["sha256"], "blur_box": box, "branding": brands, "cues": rows, "geometry_evidence": geometry["evidence"], "scale": _scale(source), "font_path": env["font"], "branding_corner_content_review": geometry.get("branding_overlaps", "NOT_ASSESSED"), "alignment": "actual final alpha bbox, tolerance 0.5px each axis"}
    save(out / "layout.json", layout)
    profile = {"fps": source["fps"], "subtitles": box}
    track = _core()[0].subtitle_track(cues, {"cues": rows}, profile, source["frames"], out / "subtitle-track")
    return layout, track


def _visual_graph(source, layout, preset, *, selected=None, showinfo=False):
    box = layout["blur_box"]
    rate = Fraction(source["fps"])
    radius = max(1, min(round(preset["blur"]["radius_at_1080"] * _scale(source)), (min(box["width"], box["height"]) - 1) // 2))
    chroma = max(0, min(radius, min(box["width"], box["height"]) // 4 - 1))
    power = int(preset["blur"]["power"])
    if power < 1:
        raise ValueError("Blur power must be positive")
    graph = [f"[0:v]trim=end_frame={source['frames']},setpts=N/({rate.numerator}/{rate.denominator}*TB),split=2[canvas][roi]", f"[roi]crop={box['width']}:{box['height']}:{box['x']}:{box['y']}:exact=1,boxblur=luma_radius={radius}:luma_power={power}:chroma_radius={chroma}:chroma_power={power}[blurred]", f"[canvas][blurred]overlay={box['x']}:{box['y']}:eof_action=pass[base]"]
    prior = "base"
    for index, key in enumerate(("logo", "wordmark"), 1):
        asset = layout["branding"][key]
        graph.extend([f"[{index}:v]format=rgba,scale={asset['width']}:{asset['height']}:flags=lanczos[brand{index}]", f"[{prior}][brand{index}]overlay={asset['x']}:{asset['y']}:eof_action=repeat:repeatlast=1[b{index}]"])
        prior = f"b{index}"
    tail = f"[{prior}][3:v]overlay={box['x']}:{box['y']}:eof_action=repeat:repeatlast=1,trim=end_frame={source['frames']},format=yuv420p"
    if showinfo:
        tail += ",showinfo=checksum=0"
    if selected is not None:
        tail += ",select='" + "+".join(f"eq(n,{n})" for n in selected) + "'"
    graph.append(tail + "[vout]")
    return ";\n".join(graph)


def _visual_inputs(source, layout, track, env):
    return [env["ffmpeg"], "-hide_banner", "-v", "error", "-xerror", "-nostdin", "-n", "-threads", "2", "-i", source["path"], "-i", layout["branding"]["logo"]["path"], "-i", layout["branding"]["wordmark"]["path"], "-f", "concat", "-safe", "0", "-i", track]


def _contact(paths, labels, out, *, width=640, columns=2):
    tiles = []
    for path, label in zip(paths, labels):
        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((width, round(width * image.height / image.width)), Image.Resampling.LANCZOS)
            tile = Image.new("RGB", (width, image.height + 30), "#151515")
            tile.paste(image, ((width - image.width) // 2, 30))
            ImageDraw.Draw(tile).text((8, 7), str(label), fill="white")
            tiles.append(tile)
    if not tiles:
        raise ValueError("No sampled images generated")
    height = max(tile.height for tile in tiles)
    sheet = Image.new("RGB", (width * columns, math.ceil(len(tiles) / columns) * height), "#151515")
    for index, tile in enumerate(tiles):
        sheet.paste(tile, ((index % columns) * width, (index // columns) * height))
    sheet.save(out, quality=92)


def representative_frames(source, cues, geometry, layout):
    result = {}
    def add(frame, reason):
        frame = int(frame)
        if not 0 <= frame < source["frames"]:
            raise ValueError(f"Representative frame outside source: {frame}")
        result.setdefault(frame, []).append(reason)
    add(0, "source-start")
    add(source["frames"] - 1, "source-end/seam-before")
    if cues:
        longest = max(cues, key=lambda cue: len(cue["canonical_text"]))
        add((longest["start_frame"] + longest["end_frame_exclusive"] - 1) // 2, "longest-text")
        for cue, row in zip(cues, layout["cues"]):
            if len(row["lines"]) == 2:
                add((cue["start_frame"] + cue["end_frame_exclusive"] - 1) // 2, "two-line-block")
                break
        add(cues[0]["start_frame"], "first-cue-onset")
    for field in ("source_high_frames", "source_low_frames", "transition_frames", "seam_frames", "representative_frames"):
        for frame in geometry.get(field, []):
            add(frame, field)
    return [{"frame": frame, "reasons": result[frame]} for frame in sorted(result)]


def preview(source, cues, geometry, preset, out, env):
    out = _folder(out)
    layout, track = _prepare(source, cues, geometry, preset, out, env)
    samples = representative_frames(source, cues, geometry, layout)
    graph = _new(out / "filtergraph.txt")
    graph.write_text(_visual_graph(source, layout, preset, selected=[row["frame"] for row in samples]), encoding="utf-8")
    _run(_visual_inputs(source, layout, track, env) + ["-filter_complex_threads", str(preset["video"]["filter_threads"]), "-/filter_complex", graph, "-map", "[vout]", "-an", "-fps_mode", "vfr", "-q:v", "2", out / "frame-%03d.jpg"], out / "logs", timeout=max(300, source["frames"] / float(Fraction(source["fps"])) * 8))
    images = sorted(out.glob("frame-*.jpg"))
    if len(images) != len(samples):
        raise ValueError("Preview did not extract every representative integer frame")
    for row, path in zip(samples, images):
        row.update(path=str(path), sha256=sha(path))
    contact = _new(out / "contact.jpg")
    _contact(images, [f"frame {row['frame']}: {', '.join(row['reasons'])}" for row in samples], contact, width=preset["preview"]["thumbnail_width"], columns=preset["preview"]["contact_columns"])
    manifest = {"status": "AWAITING_VISUAL_REVIEW", "source_sha256": source["sha256"], "layout": str(out / "layout.json"), "layout_sha256": sha(out / "layout.json"), "contact": str(contact), "contact_sha256": sha(contact), "samples": samples, "source_high_low_transition_evidence": {key: bool(geometry.get(key)) for key in ("source_high_frames", "source_low_frames", "transition_frames")}, "note": "Only actual review of these hash-bound previews may approve long rendering. No listening or semantic acceptance."}
    save(out / "preview-manifest.json", manifest)
    return {"manifest": str(out / "preview-manifest.json"), "contact": str(contact), "layout": str(out / "layout.json"), "contact_sha256": manifest["contact_sha256"], "samples": samples, "files": [str(path) for path in out.rglob("*") if path.is_file()]}


def render_video(source, cues, geometry, preset, out, env):
    out = _folder(out)
    layout, track = _prepare(source, cues, geometry, preset, out, env)
    graph = _new(out / "filtergraph.txt")
    graph.write_text(_visual_graph(source, layout, preset), encoding="utf-8")
    output = _new(out / "video-only.mp4")
    cfg = preset["video"]
    rate = Fraction(source["fps"])
    stream = _video(source, env)
    if stream.get("pix_fmt") != "yuv420p" or stream.get("color_transfer") in ("smpte2084", "arib-std-b67"):
        raise ValueError("Unreviewed native pixel/HDR conversion is forbidden")
    if source["width"] % 2 or source["height"] % 2:
        raise ValueError("Native yuv420p canvas must have even dimensions")
    argv = _visual_inputs(source, layout, track, env) + ["-filter_complex_threads", str(cfg["filter_threads"]), "-/filter_complex", graph, "-map", "[vout]", "-an", "-frames:v", str(source["frames"]), "-fps_mode", "passthrough", "-c:v", cfg["codec"], "-preset", cfg["preset"], "-threads", str(cfg["threads"]), "-crf", str(cfg["crf"]), "-maxrate", cfg["maxrate"], "-bufsize", cfg["bufsize"], "-pix_fmt", cfg["pixel_format"], "-force_key_frames", "0", "-video_track_timescale", str(rate.numerator)]
    for field, option in (("color_range", "-color_range"), ("color_space", "-colorspace"), ("color_transfer", "-color_trc"), ("color_primaries", "-color_primaries")):
        if stream.get(field) not in (None, "unknown", "unspecified"):
            argv += [option, stream[field]]
    if stream.get("sample_aspect_ratio", "1:1") not in ("1:1", "N/A"):
        # setdar would alter display geometry. Preserve the original sample ratio.
        dar = Fraction(source["width"], source["height"]) * Fraction(stream["sample_aspect_ratio"].replace(":", "/"))
        argv += ["-aspect", f"{dar.numerator}:{dar.denominator}"]
    if stream.get("color_range") in ("tv", "pc"):
        argv += ["-bsf:v", f"h264_metadata=video_full_range_flag={int(stream['color_range'] == 'pc')}"]
    receipt = _run(argv + META + [output], out / "logs", timeout=max(600, source["frames"] / float(rate) * 25))
    result = {"output": str(output), "video": str(output), "sha256": sha(output), "layout": str(out / "layout.json"), "command_receipt": receipt, "frames": source["frames"], "fps": source["fps"], "qa_policy": "No duplicate full decode here; consolidated final QA is mandatory", "files": [str(path) for path in out.rglob("*") if path.is_file()]}
    result["files"].append(str(out / "render.json"))
    save(out / "render.json", result)
    return result


def tail_alignment(original_samples, expected_samples, maximum_silence_samples=1600):
    if any(type(n) is not int or n < 0 for n in (original_samples, expected_samples, maximum_silence_samples)) or not expected_samples:
        raise ValueError("Invalid source/audio sample count")
    missing = expected_samples - original_samples
    if missing < 0:
        raise ValueError("Source audio exceeds native video clock; explicit scope correction required")
    if missing > maximum_silence_samples:
        raise ValueError("Source tail shortfall exceeds verified rule; do not hide missing dialogue with silence")
    return {"original_resampled_source_samples": original_samples, "expected_samples": expected_samples, "silence_appended_samples": missing, "silence_ms": missing / 48, "maximum_allowed_samples": maximum_silence_samples, "policy": "Append only measured terminal silence; preserve every original sample, no shifting, no speed change"}


def _verify_silent_tail(original, padded, original_info, padded_info):
    """Check bytes: apad may add only zeros after unchanged original float PCM."""
    with Path(original).open("rb") as before, Path(padded).open("rb") as after:
        before.seek(original_info["data_offset"])
        after.seek(padded_info["data_offset"])
        remaining = original_info["data_bytes"]
        while remaining:
            length = min(384000, remaining)
            if before.read(length) != after.read(length):
                raise ValueError("Terminal padding altered existing source float samples")
            remaining -= length
        remaining = padded_info["data_bytes"] - original_info["data_bytes"]
        while remaining:
            length = min(384000, remaining)
            if after.read(length) != b"\0" * length:
                raise ValueError("Appended source tail is not exact zero PCM")
            remaining -= length


def source_audio_scope(original_samples, expected_samples, explicit_scope, maximum_out_of_scope_samples=4096):
    if any(type(n) is not int or n < 0 for n in (original_samples, expected_samples, maximum_out_of_scope_samples)) or not expected_samples:
        raise ValueError("Invalid native audio scope/sample count")
    extra = max(0, original_samples - expected_samples)
    if extra and (explicit_scope != "native_video" or extra > maximum_out_of_scope_samples):
        raise ValueError("Audio extends outside explicit native video scope beyond the verified bound; preserve full PCM and review scope")
    return {"full_resampled_source_samples": original_samples, "native_video_samples": expected_samples, "selected_source_samples": min(original_samples, expected_samples), "discarded_OUT_OF_VIDEO_SCOPE_samples": extra, "maximum_out_of_scope_samples": maximum_out_of_scope_samples, "explicit_scope": explicit_scope, "reason": "Select only the native video interval without changing its sample prefix. Full resampled PCM is preserved; no assertion that out-of-scope samples are AAC padding or silent."}


def _verify_scope_prefix(original, scoped, original_info, scoped_info):
    """Verify every selected float byte and hash the retained out-of-scope tail."""
    selected_bytes = scoped_info["data_bytes"]
    if selected_bytes > original_info["data_bytes"]:
        raise ValueError("Scope selection cannot invent samples")
    with Path(original).open("rb") as before, Path(scoped).open("rb") as after:
        before.seek(original_info["data_offset"])
        after.seek(scoped_info["data_offset"])
        remaining = selected_bytes
        while remaining:
            length = min(384000, remaining)
            if before.read(length) != after.read(length):
                raise ValueError("Native scope selection altered existing float samples")
            remaining -= length
        remaining = original_info["data_bytes"] - selected_bytes
        digest = hashlib.sha256()
        while remaining:
            block = before.read(min(384000, remaining))
            if not block:
                raise ValueError("Full source PCM tail disappeared")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def validate_separation_response(response, request, expected):
    included=request['config']['included_stems']
    if (response.get('technical_status')!='PASS' or response.get('input_samples')!=expected or response.get('output_samples')!=expected
            or response.get('included_stems')!=included
            or response.get('excluded_stems')!=[s for s in ['music','sfx','speech'] if s not in included]
            or response.get('sources')!=['music','sfx','speech'] or response.get('precision')!='FP32'
            or response.get('engine')!='MVSEP-CDX23' or response.get('shifts')!=1 or response.get('overlap')!=.8
            or response.get('source_sha256')!=request['source_sha256']
            or response.get('model_sha256')!=sha(request['config']['checkpoint'])
            or response.get('output_sha256')!=sha(response['output'])):
        raise ValueError('Separation engine/timeline/stem binding failed; retained response.json')
    if set(response.get('stems',{}))!={'music','sfx','speech'}:raise ValueError('Separation stem receipt incomplete')
    for stem in response['stems'].values():
        if sha(stem['path'])!=stem['sha256']:raise ValueError('Separation stem hash differs')


def separate(source, out, env, config):
    included = config.get('included_stems')
    if included not in [['sfx'], ['music', 'sfx']]:
        raise ValueError('Explicit included_stems must be sfx or music+sfx')
    if config.get('precision')!='FP32' or config.get('shifts')!=1 or config.get('overlap')!=.8:
        raise ValueError('Supported separation policy requires FP32, shifts=1, overlap=0.8')
    out = _folder(out)
    _, media3 = _core()
    pcm = _new(out / "source-resampled-float48.wav")
    _run([env["ffmpeg"], "-v", "error", "-xerror", "-nostdin", "-n", "-i", source["path"], "-map", "0:a:0", "-vn", "-af", "aresample=48000,asetpts=N/SR/TB", "-ar", "48000", "-ac", "2", "-c:a", "pcm_f32le", "-rf64", "auto", pcm], out / "logs")
    info = media3.pcm_header(pcm, channels=2, subtype="FLOAT")
    expected = samples_at_frame(source["frames"], source["fps"])
    scope = source_audio_scope(info["samples"], expected, source.get("audio_scope"), config.get("maximum_out_of_video_scope_audio_samples", 4096))
    scope.update(full_pcm=str(pcm), full_pcm_sha256=sha(pcm), source_sha256=source["sha256"])
    if scope["discarded_OUT_OF_VIDEO_SCOPE_samples"]:
        if config.get("audio_scope") != "native_video":
            raise ValueError("Separation preset must explicitly select native_video audio scope")
        scoped = _new(out / "source-native-scope-float48.wav")
        _run([env["ffmpeg"], "-v", "error", "-xerror", "-nostdin", "-n", "-i", pcm, "-af", f"atrim=end_sample={expected},asetpts=N/SR/TB", "-c:a", "pcm_f32le", "-rf64", "auto", scoped], out / "logs")
        scoped_info = media3.pcm_header(scoped, channels=2, samples=expected, subtype="FLOAT")
        scope.update(selected_prefix_byte_identical=True, discarded_float_payload_sha256=_verify_scope_prefix(pcm, scoped, info, scoped_info), discarded_interval_samples_48k=[expected, info["samples"]], scoped_pcm=str(scoped), scoped_pcm_sha256=sha(scoped))
        pcm, info = scoped, scoped_info
    save(out / "source-audio-scope.json", scope)
    alignment = tail_alignment(info["samples"], expected, config.get("maximum_source_tail_silence_samples", 1600))
    alignment.update(source_sha256=source["sha256"], original_pcm_sha256=sha(pcm), full_resampled_source_samples=scope["full_resampled_source_samples"], source_audio_scope_receipt=str(out / "source-audio-scope.json"))
    if alignment["silence_appended_samples"]:
        padded = _new(out / "source-aligned-float48.wav")
        _run([env["ffmpeg"], "-v", "error", "-xerror", "-nostdin", "-n", "-i", pcm, "-af", f"apad=whole_len={expected},atrim=end_sample={expected},asetpts=N/SR/TB", "-c:a", "pcm_f32le", "-rf64", "auto", padded], out / "logs")
        padded_info = media3.pcm_header(padded, channels=2, samples=expected, subtype="FLOAT")
        _verify_silent_tail(pcm, padded, info, padded_info)
        alignment["existing_float_samples_byte_identical"] = True
        alignment["appended_tail_exact_zero"] = True
        pcm = padded
    media3.pcm_header(pcm, channels=2, samples=expected, subtype="FLOAT")
    alignment["aligned_pcm_sha256"] = sha(pcm)
    save(out / "source-tail-alignment.json", alignment)
    stems = out / "stems"
    stems.mkdir()
    cfg = {**config, "checkpoint": config.get("checkpoint", env.get("separation_checkpoint")), "cupti_library": config.get("cupti_library", env.get("cupti_library")), "dll_dirs": env.get("separation_dll_directories", []), "ffmpeg": env["ffmpeg"]}
    if not cfg["checkpoint"] or not cfg["cupti_library"]:
        raise ValueError("Explicit separation checkpoint and CUPTI path required")
    request = {"mode": "separate", "source": str(pcm), "source_sha256": sha(pcm), "config": cfg, "lock_path": env["separation_engine_lock"], "output_dir": str(stems)}
    save(out / "request.json", request)
    tools = ROOT / "vendor/core/tools"
    _run([env["separation_python"], tools / "fresh_v3_guard.py", "--lock", env["gpu_lock"], "--wait-seconds", str(config.get("gpu_wait_seconds", 7200)), tools / "fresh_separation_v3_worker.py", "--request", out / "request.json", "--response", out / "response.json"], out / "logs", timeout=config.get("timeout_seconds", 7200))
    response = json.loads((out / "response.json").read_text(encoding="utf-8"))
    validate_separation_response(response,request,expected)
    media3.pcm_header(response["output"], channels=2, samples=expected, subtype="FLOAT")
    return {"output": response["output"], "background_wav": response["output"], "sha256":sha(response['output']), "receipt": str(out / "response.json"), 'included_stems':included, 'excluded_stems':response['excluded_stems'], "source_audio_scope": str(out / "source-audio-scope.json"), "source_tail_alignment": str(out / "source-tail-alignment.json"), "technical_PASS": True, "files": [str(path) for path in out.rglob("*") if path.is_file()]}


def mix(source, voice_wav, background_wav, out, env, preset):
    out = _folder(out)
    _, media3 = _core()
    expected = samples_at_frame(source["frames"], source["fps"])
    media3.pcm_header(voice_wav, channels=1, samples=expected)
    media3.pcm_header(background_wav, channels=2, samples=expected, subtype="FLOAT")
    cfg = preset["mix"]
    if cfg["sample_rate"] != RATE or cfg["master_subtype"] != "PCM_16" or cfg["limiter_latency_compensation"] is not True:
        raise ValueError("Unverified master clock/subtype/limiter latency policy")
    graph = f"[0:a]aformat=sample_fmts=flt,volume={cfg['background_db']}dB[bg];[1:a]aformat=sample_fmts=flt,pan=stereo|c0=c0|c1=c0,volume={cfg['voice_db']}dB[voice];[bg][voice]amix=inputs=2:duration=first:normalize=0:dropout_transition=0,alimiter=limit={cfg['limiter_limit']}:level={int(cfg['limiter_auto_level'])}:latency=1,apad=whole_len={expected},atrim=end_sample={expected},asetpts=N/SR/TB[a]"
    output = _new(out / "master48.wav")
    _run([env["ffmpeg"], "-v", "error", "-xerror", "-nostdin", "-n", "-i", background_wav, "-i", voice_wav, "-filter_complex", graph, "-map", "[a]", "-vn", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", "-rf64", "auto", output], out / "logs")
    media3.pcm_header(output, channels=2, samples=expected)
    result = {"output": str(output), "audio_master": str(output), "sha256": sha(output), "samples": expected, "filtergraph": graph, "float_until_final_PCM16": True, "listening_review": "NOT_PERFORMED", "files": [str(output)]}
    result["files"] = [str(path) for path in out.rglob("*") if path.is_file()] + [str(out / "mix.json")]
    save(out / "mix.json", result)
    return result


def offset_cues(episodes):
    if not episodes:
        raise ValueError("Finalization requires explicit episode scope")
    rate = Fraction(episodes[0]["source"]["fps"])
    offset = 0
    cues, seams = [], []
    for index, episode in enumerate(episodes):
        source = episode["source"]
        if Fraction(source["fps"]) != rate:
            raise ValueError("Differing episode frame clocks need an explicit conversion decision")
        _validate_cues(episode["cues"], source)
        if index:
            seams.append({"frame": offset, "sample": samples_at_frame(offset, str(rate)), "episode_index": index})
        for cue in episode["cues"]:
            cues.append({**cue, "episode_index": index, "source_start_frame": cue["start_frame"], "source_end_frame_exclusive": cue["end_frame_exclusive"], "start_frame": offset + cue["start_frame"], "end_frame_exclusive": offset + cue["end_frame_exclusive"]})
        offset += source["frames"]
    return cues, seams, offset, str(rate)


def _srt_time(frame, fps):
    # Independent rounding at every boundary; no accumulated decimal offsets.
    ms = round(Fraction(frame * 1000, 1) / Fraction(fps))
    hours, rem = divmod(ms, 3600000)
    minutes, rem = divmod(rem, 60000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def balanced(terms):
    """Bound expression depth without changing selected frame indices."""
    if not terms:
        return "0"
    if len(terms) == 1:
        return terms[0]
    middle = len(terms) // 2
    return "(" + balanced(terms[:middle]) + "+" + balanced(terms[middle:]) + ")"


def _final_decode(output, frames, fps, expected, samples, out, env):
    """One full FFmpeg pass decodes video+audio and writes representative frames.

    showinfo reports integer PTS before select. PCM is reduced in bounded blocks;
    no second full decode, ffprobe frame pass, or decoded WAV is needed.
    """
    out.mkdir()
    expr = balanced([f"eq(n,{frame})" for frame in samples])
    graph = f"[0:v]showinfo=checksum=0,select='{expr}'[sample]"
    args = [env["ffmpeg"], "-hide_banner", "-v", "info", "-xerror", "-nostdin", "-n", "-i", output, "-filter_complex", graph, "-map", "[sample]", "-an", "-fps_mode", "vfr", "-q:v", "2", out / "frame-%03d.jpg", "-map", "0:a:0", "-vn", "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "2", "-ar", "48000", "pipe:1"]
    args = list(map(str, args))
    started = time.monotonic()
    log = out / "decode.stderr.log"
    count = 0
    peak = energy = 0.0
    with log.open("wb") as stderr:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=stderr)
        timer = threading.Timer(max(300, expected / RATE * 10), process.kill)
        timer.start()
        remainder = b""
        try:
            while block := process.stdout.read(384000):
                block = remainder + block
                usable = len(block) // 8 * 8
                remainder = block[usable:]
                values = array("f")
                values.frombytes(block[:usable])
                if sys.byteorder != "little":
                    values.byteswap()
                measured = np.asarray(values, dtype=np.float64)
                if not np.isfinite(measured).all():
                    raise ValueError("Non-finite final decoded audio")
                if measured.size:
                    peak = max(peak, float(np.max(np.abs(measured))))
                    energy += float(np.dot(measured, measured))
                count += len(values) // 2
            process.wait()
            if process.returncode or remainder:
                raise ValueError(f"Consolidated final decode failed; see {log}")
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
    tb, actual = None, 0
    with log.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if "showinfo" not in line:
                continue
            match = re.search(r"config in time_base:\s*(\d+/\d+)", line)
            if match:
                tb = Fraction(match[1])
            match = re.search(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:", line)
            if match:
                index, pts = map(int, match.groups())
                if tb is None or index != actual or pts * tb != Fraction(actual, 1) / Fraction(fps):
                    raise ValueError(f"Final frame clock discontinuity at {actual}")
                actual += 1
    if actual != frames or not 0 <= count - expected < 1024:
        raise ValueError(f"Final decode counts differ: video {actual}/{frames}, audio {count}/{expected}")
    images = sorted(out.glob("frame-*.jpg"))
    if len(images) != len(samples):
        raise ValueError("Missing final seam/sample frame")
    receipt = {"status": "PASS", "argv": args, "wall_seconds": time.monotonic() - started, "video_frames": actual, "all_frame_PTS": "PASS", "decoded_audio_samples": count, "expected_audio_samples": expected, "aac_padding_samples": count - expected, "audio_peak": peak, "audio_rms": math.sqrt(energy / max(1, count * 2)), "single_full_decode_pass": True, "sample_images": [{"frame": frame, "path": str(path), "sha256": sha(path)} for frame, path in zip(samples, images)]}
    save(out / "decode.json", receipt)
    return receipt, images


def finalize(episodes, out, env, preset):
    out = _folder(out)
    _, media3 = _core()
    cues, seams, frames, fps = offset_cues(episodes)
    sources = [episode["source"] for episode in episodes]
    width, height = sources[0]["width"], sources[0]["height"]
    streams = [_video(source, env) for source in sources]
    descriptors = ("width", "height", "pix_fmt", "sample_aspect_ratio", "color_range", "color_space", "color_transfer", "color_primaries")
    if any((source["width"], source["height"]) != (width, height) for source in sources):
        raise ValueError("Native episode dimensions differ; no implicit scaling")
    if any(any(stream.get(key) != streams[0].get(key) for key in descriptors) for stream in streams[1:]):
        raise ValueError("Native episode visual descriptors differ")
    clips, masters = [], []
    expected = samples_at_frame(frames, fps)
    if sum(samples_at_frame(source["frames"], fps) for source in sources) != expected:
        raise ValueError("Independent episode PCM clocks do not sum to combined frame clock")
    for episode in episodes:
        video, master = episode["video"], episode["audio_master"]
        video = video["output"] if isinstance(video, dict) else video
        master = master["output"] if isinstance(master, dict) else master
        media3.pcm_header(master, channels=2, samples=samples_at_frame(episode["source"]["frames"], fps))
        clips.append({"path": str(video), "sha256": sha(video)})
        masters.append(master)
    joined = _new(out / "joined-video.mp4")
    media3.concat_video(clips, joined, env["ffmpeg"], out)
    master = _new(out / "continuous-master48.wav")
    if media3.concat_pcm(masters, master, 2) != expected:
        raise ValueError("Final PCM concatenation count differs")
    master_info = media3.pcm_header(master, channels=2, samples=expected)
    # Exact bytes across both sides of every PCM join, without decoding again.
    for index, seam in enumerate(seams):
        left = media3.pcm_header(masters[index], channels=2)
        right = media3.pcm_header(masters[index + 1], channels=2)
        before, after = min(1024, left["samples"]), min(1024, right["samples"])
        with Path(masters[index]).open("rb") as l, Path(masters[index + 1]).open("rb") as r, master.open("rb") as combined:
            l.seek(left["data_offset"] + (left["samples"] - before) * left["block_align"])
            r.seek(right["data_offset"])
            expected_bytes = l.read(before * left["block_align"]) + r.read(after * right["block_align"])
            combined.seek(master_info["data_offset"] + (seam["sample"] - before) * master_info["block_align"])
            if combined.read(len(expected_bytes)) != expected_bytes:
                raise ValueError("PCM seam bytes differ from ordered episode masters")
        seam.update(PCM_join="BYTE_IDENTICAL", checked_before_samples=before, checked_after_samples=after, window_sha256=hashlib.sha256(expected_bytes).hexdigest())
    output = _new(out / "sample-DRAFT.mp4")
    _run([env["ffmpeg"], "-v", "error", "-xerror", "-nostdin", "-n", "-i", joined, "-i", master, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", preset["mix"]["aac_bitrate"], "-ar", "48000", "-ac", "2", "-movie_timescale", "48000", "-movflags", "+faststart", *META, output], out / "logs")
    srt = _new(out / "English.srt")
    srt.write_text("\n\n".join(f"{index}\n{_srt_time(cue['start_frame'], fps)} --> {_srt_time(cue['end_frame_exclusive'], fps)}\n{cue['canonical_text']}" for index, cue in enumerate(cues, 1)) + "\n", encoding="utf-8")
    # Parse the serialized artifact, rather than accepting its writer's state.
    import srt as srt_module
    parsed = list(srt_module.parse(srt.read_text(encoding="utf-8")))
    if len(parsed) != len(cues):
        raise ValueError("Combined SRT coverage differs")
    for row, cue in zip(parsed, cues):
        if row.content != cue["canonical_text"] or abs(row.start.total_seconds() - float(Fraction(cue["start_frame"], 1) / Fraction(fps))) > .000501 or abs(row.end.total_seconds() - float(Fraction(cue["end_frame_exclusive"], 1) / Fraction(fps))) > .000501:
            raise ValueError("Serialized SRT text/offset mismatch")
    probe = _probe(output, env["ffprobe"])
    save(out / "final-probe.json", probe)
    videos = [row for row in probe["streams"] if row["codec_type"] == "video"]
    audios = [row for row in probe["streams"] if row["codec_type"] == "audio"]
    if len(videos) != 1 or len(audios) != 1 or probe.get("chapters"):
        raise ValueError("Final stream/chapter topology differs")
    video, audio = videos[0], audios[0]
    if (video["codec_name"], video["width"], video["height"], video["pix_fmt"]) != ("h264", width, height, "yuv420p") or Fraction(video["avg_frame_rate"]) != Fraction(fps) or Fraction(video["r_frame_rate"]) != Fraction(fps):
        raise ValueError("Final native video descriptor differs")
    for key in descriptors[3:]:
        original = streams[0].get(key)
        if original not in (None, "unknown", "unspecified", "N/A") and video.get(key) != original:
            raise ValueError(f"Native visual descriptor changed: {key}")
    duration_samples = Fraction(audio["duration_ts"]) * Fraction(audio["time_base"]) * RATE
    if (audio["codec_name"], int(audio["sample_rate"]), audio["channels"]) != ("aac", RATE, 2) or duration_samples != expected or any(Fraction(row.get("start_time", "0")) != 0 for row in (video, audio)):
        raise ValueError("Final audio clock/stream format differs")
    points = {0, min(frames - 1, round(float(Fraction(fps)))), frames // 2, frames - 1}
    for seam in seams:
        points.update(max(0, min(frames - 1, seam["frame"] + delta)) for delta in (-1, 0, 1))
    points = sorted(points)
    decode, images = _final_decode(output, frames, fps, expected, points, out / "final-qa", env)
    contact = _new(out / "contact.jpg")
    _contact(images, [f"final frame {frame}" for frame in points], contact, width=preset["preview"]["thumbnail_width"], columns=preset["preview"]["contact_columns"])
    result = {"status": "DRAFT", "technical_PASS": True, "output": str(output), "sha256": sha(output), "srt": str(srt), "contact": str(contact), "audio_master": str(master), "frames": frames, "fps": fps, "pcm_samples": expected, "seams": seams, "canonical_cues": len(cues), "aac_encoded_once_after_PCM_concat": True, "decode": decode, "metadata_probe": str(out / "final-probe.json"), "metadata_policy": "Automatic inheritance disabled; generated muxer/encoder tags may remain", "independent_semantics": "NOT_PERFORMED", "independent_listening": "NOT_PERFORMED", "sample_visual_review": "AWAITING_REVIEW", "files": [str(path) for path in out.rglob("*") if path.is_file()]}
    result["qa"] = str(out / "qa.json")
    result["files"].append(result["qa"])
    save(out / "qa.json", result)
    return result
