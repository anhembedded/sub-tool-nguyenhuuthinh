"""Isolated, fail-closed native OCR and cinematic separation for Fresh Run.

This module uses only the standard library. Engine imports happen in a separate
interpreter selected explicitly by the caller, with process-local DLL paths.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


class EngineError(RuntimeError):
    """Engine unavailable, locked input changed, or technical QA failed."""


def sha256(path: Path | str) -> str:
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def check_hash(path: Path | str, expected: str) -> None:
    if len(expected) != 64 or sha256(path) != expected:
        raise EngineError(f"SHA-256 mismatch: {path}")


def validate_roi(roi: dict, film_id: str, source_sha256: str,
                 width: int, height: int, frames: int) -> tuple[int, int, int, int]:
    if roi.get("film_id") != film_id or roi.get("source_sha256") != source_sha256:
        raise EngineError("ROI must be verified for this film and source hash")
    values = tuple(roi.get(k) for k in ("x", "y", "width", "height"))
    if any(type(x) is not int for x in values):
        raise EngineError("ROI coordinates must be native integer pixels")
    x, y, w, h = values
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > width or y+h > height:
        raise EngineError("ROI outside native frame")
    proof = roi.get("verification", {})
    if proof.get("status") != "VERIFIED" or not proof.get("evidence"):
        raise EngineError("Per-film ROI visual verification and evidence required")
    for item in proof["evidence"]:
        frame = item.get("frame_index")
        if type(frame) is not int or not 0 <= frame < frames:
            raise EngineError("ROI evidence frame outside selected scope")
        check_hash(Path(item["path"]), item["sha256"])
    return values


def segments_from_frames(rows: list[dict], namespace: str) -> list[dict]:
    """Keep every observed text transition at its native frame, without fuzzing."""
    segments = []
    for frame, row in enumerate(rows):
        if row["frame_index"] != frame:
            raise EngineError("Frame sequence is incomplete or reordered")
        text = row["text"]
        if segments and segments[-1]["source_text"] == text and segments[-1]["end_frame_exclusive"] == frame:
            segments[-1]["end_frame_exclusive"] = frame + 1
            segments[-1]["frame_lineage"].append(frame)
        elif text:
            segments.append({"cue_id": f"{namespace}_f{frame:09d}",
                             "source_text": text, "chinese_text": text,
                             "start_frame": frame, "end_frame_exclusive": frame+1,
                             "frame_lineage": [frame], "review_status": "NEEDS_REVIEW"})
    return segments


class _Adapter:
    mode = ""

    def __init__(self, config: dict, lock_path: Path | str | None = None):
        self.config = dict(config)
        self.project = Path(__file__).resolve().parents[2]
        self.lock_path = Path(lock_path or self.project / "config/fresh-engine-lock.json").resolve()

    def _run(self, source: Path, output_dir: Path, **extra) -> dict:
        source, output_dir = Path(source).resolve(strict=True), Path(output_dir).resolve()
        if source == output_dir or output_dir in source.parents:
            raise EngineError("Engine output must not enclose the source")
        if output_dir.exists():
            raise EngineError("Choose a new engine output version; existing attempts are immutable")
        for p in [output_dir, *output_dir.parents]:
            if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
                raise EngineError("Output through symlink/junction is forbidden")
        output_dir.mkdir(parents=True)
        interpreter = Path(self.config.get("python", ""))
        if not interpreter.is_file():
            raise EngineError("Explicit isolated engine Python is required")
        attempts = self.config.get("attempts", 1)
        if type(attempts) is not int or not 1 <= attempts <= 3:
            raise EngineError("Engine attempts must be between 1 and 3")
        started = time.monotonic()
        for attempt in range(1, attempts+1):
            target = output_dir / f"attempt-{attempt:02d}"
            target.mkdir()
            request = {"mode": self.mode, "source": str(source),
                       "source_sha256": sha256(source), "config": self.config,
                       "lock_path": str(self.lock_path), "output_dir": str(target), **extra}
            write_json(target / "request.json", request)
            argv = [str(interpreter), "-B", str(self.project / "tools/fresh_engine_worker.py"),
                    "--request", str(target / "request.json")]
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
            began = time.monotonic()
            with (target / "stdout.log").open("w", encoding="utf-8") as out, (target / "stderr.log").open("w", encoding="utf-8") as err:
                try:
                    proc = subprocess.run(argv, stdout=out, stderr=err, env=env,
                                          timeout=self.config.get("timeout_seconds", 3600),
                                          creationflags=0x08000000 if os.name == "nt" else 0)
                    code, timeout = proc.returncode, False
                except subprocess.TimeoutExpired:
                    code, timeout = None, True
            write_json(target / "process.json", {"argv": argv, "returncode": code,
                       "timeout": timeout, "wall_seconds": time.monotonic()-began})
            receipt = target / "receipt.json"
            if code == 0 and receipt.is_file():
                result = json.loads(receipt.read_text(encoding="utf-8"))
                if result.get("technical_status") == "PASS":
                    result["receipt_path"] = str(receipt)
                    result["adapter_wall_seconds"] = time.monotonic()-started
                    return result
        raise EngineError(f"{self.mode} failed; evidence retained at {output_dir}")


class PaddleOCRAdapter(_Adapter):
    mode = "ocr"

    def run(self, source: Path, output_dir: Path, *, film_id: str, roi: dict,
            expected_source_sha256: str) -> dict:
        check_hash(source, expected_source_sha256)
        return self._run(source, output_dir, film_id=film_id, roi=roi)


class MVSEPAdapter(_Adapter):
    mode = "separate"

    def run(self, source_pcm: Path, output_dir: Path) -> dict:
        return self._run(source_pcm, output_dir)
