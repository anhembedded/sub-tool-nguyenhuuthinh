"""Opt-in, local Kokoro adapter with explicit pronunciation and review lineage.

Only technical checks run here. A WAV, a valid phoneme, or a successful model call
never constitutes listening, pronunciation, or semantic acceptance. The adapter
uses KPipeline G2P/tokenization, then feeds checked phoneme chunks to KModel via
generate_from_tokens; it avoids the pipeline paths that truncate at 510 symbols.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from importlib import metadata
import inspect
import json
from pathlib import Path
import re
import subprocess
import time
import uuid
import wave

from .storage import atomic_json, file_hash, guard_output, object_hash, process_lock, read_json

ADAPTER_VERSION = "fresh-kokoro-v2.2"
ADAPTER_SOURCE_SHA256 = file_hash(Path(__file__))
MODEL_ID = "hexgrad/Kokoro-82M"
LANGUAGES = {"a": "American English", "b": "British English", "e": "Spanish",
             "f": "French", "h": "Hindi", "i": "Italian", "p": "Brazilian Portuguese",
             "j": "Japanese", "z": "Mandarin Chinese"}
CONTEXT_CATEGORIES = ("shushing", "interjections", "abbreviations", "numbers",
                      "names", "homographs", "punctuation", "delivery")
OVERRIDE_TYPES = {"proper_name", "term", "abbreviation", "number", "homograph", "interjection"}
SOURCES = [
    "https://github.com/hexgrad/kokoro/blob/main/kokoro/pipeline.py",
    "https://github.com/hexgrad/kokoro/blob/main/kokoro/model.py",
    "https://github.com/hexgrad/misaki/blob/main/misaki/en.py",
    "https://huggingface.co/hexgrad/Kokoro-82M/blob/main/README.md",
]


class NeedsReview(ValueError):
    """Evidence is incomplete or unresolved; preserve the cue for review."""


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def engine_capabilities() -> dict:
    return {"adapter_version": ADAPTER_VERSION, "model": MODEL_ID,
            "native_sample_rate": 24000, "controls": ["voice", "language", "speed", "phonemes"],
            "unsupported": ["free_form_delivery_prompt", "emotion", "whisper", "SSML", "pitch"],
            "pronunciation_markup": "Misaki supports [surface](/phonemes/), but this adapter uses checked token spans and raw phonemes",
            "implemented_languages": ["a", "b"],
            "language_limit": "Other published voice languages can be listed but require a separately verified G2P alignment adapter",
            "acting_limit": "Delivery labels are retained as evidence; no acting/emotion control is sent",
            "sources": SOURCES}


def _checked_file(row: dict, label: str) -> Path:
    if not isinstance(row, dict) or not isinstance(row.get("path"), str):
        raise ValueError(f"Missing locked local {label} path")
    path = Path(row["path"])
    expected = row.get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError(f"Missing full SHA-256 for {label}")
    if not path.is_file() or file_hash(path) != expected:
        raise ValueError(f"Missing/corrupt locked {label}: {path}")
    return path.resolve()


def list_voices(model_lock: dict, language: str | None = None) -> list[dict]:
    """List only locally provisioned, SHA-verified packs; no invented catalogue."""
    result = []
    for name, row in sorted(model_lock.get("voices", {}).items()):
        if not re.fullmatch(r"[abefhijpz][fm]_[a-z0-9_]+", name):
            raise ValueError(f"Invalid voice ID: {name}")
        if language and not name.startswith(language):
            continue
        path = _checked_file(row, f"voice {name}")
        result.append({"voice": name, "language": name[0], "language_name": LANGUAGES[name[0]],
                       "sha256": row["sha256"], "path": str(path), "available": True,
                       "adapter_supported": name[0] in "ab"})
    return result


def review_candidates(text: str) -> list[dict]:
    """Conservative review routing, never a contextual interpretation/approval."""
    patterns = {
        "shushing": r"\b(?:shh+|shush)\b",
        "interjections": r"\b(?:hmm+|huh|ah|oh|uh|um|ha(?:ha)*|tsk)\b",
        "abbreviations": r"\b(?:[A-Z]{2,}|A|I|No\.|Dr\.|St\.)",
        "numbers": r"(?:[$£€]?\d[\d.,:/%–\-]*|\b(?:kg|km|mph|cm|mm)\b)",
        "homographs": r"\b(?:read|lead|live|bow|wind|tear)\b",
    }
    return [{"category": category, "surface": match.group(), "span": list(match.span()),
             "status": "NEEDS_REVIEW", "meaning_inferred": False}
            for category, pattern in patterns.items()
            for match in re.finditer(pattern, text, re.I if category != "abbreviations" else 0)]


def validate_cue(cue: dict, require_context: bool = True) -> dict:
    for field in ("cue_id", "source_text", "canonical_text", "glossary_version", "pronunciation_rules_version"):
        if not isinstance(cue.get(field), str) or not cue[field].strip():
            raise ValueError(f"Missing cue field {field}")
    text = cue["canonical_text"]
    if text != text.strip() or any(ord(c) < 32 for c in text) or any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        raise ValueError("canonical_text must be trimmed Unicode scalar text without control characters")
    if re.search(r"\[[^\]]+\]\(/|<\s*/?(?:speak|prosody|phoneme)\b", text, re.I):
        raise ValueError("Technical markup cannot appear in canonical_text")
    if re.search(r"(?:\[(?:whisper|sadly|angry|quiet|shushing)\]|\((?:whisper|sadly|angrily)\))", text, re.I):
        raise ValueError("Move delivery instructions out of canonical_text")
    previous_end = 0
    for row in cue.get("pronunciation_overrides", []):
        span = row.get("span")
        if (not isinstance(span, list) or len(span) != 2 or
                any(type(v) is not int for v in span)):
            raise ValueError("Override span must be [start,end] Unicode code point integers")
        start, end = span
        if not (previous_end <= start < end <= len(text)) or text[start:end] != row.get("surface"):
            raise ValueError("Stale, overlapping, unsorted or non-codepoint pronunciation span")
        previous_end = end
        if row.get("type") not in OVERRIDE_TYPES:
            raise ValueError("Unsupported pronunciation override type")
        if not isinstance(row.get("phonemes"), str) or not row["phonemes"].strip():
            raise NeedsReview("A context-chosen phoneme string is required; reading text alone is not a phoneme")
        if not row.get("reason") or not row.get("evidence"):
            raise NeedsReview("Pronunciation choice requires reason and context evidence")
        if row.get("engine_supported") is not True:
            raise NeedsReview("Pronunciation override is unsupported/unverified by the engine")
        if row.get("listening_status") not in {"PENDING", "APPROVED", "REJECTED"}:
            raise ValueError("Missing override listening status")
        if row.get("listening_status") == "REJECTED":
            raise NeedsReview("Rejected pronunciation must be corrected before synthesis")
        if row.get("listening_status") == "APPROVED" and not row.get("listening_receipt"):
            raise ValueError("Listening approval requires an actual receipt")
    # A confirmed intent alone does not prove how the engine will pronounce a
    # short shushing/letter sequence. Require an explicit per-occurrence choice
    # for both a silence signal and an initialism; never trust default G2P here.
    for candidate in review_candidates(text):
        if candidate["category"] == "shushing" and not any(
                row["span"] == candidate["span"] and row["surface"] == candidate["surface"]
                for row in cue.get("pronunciation_overrides", [])):
            raise NeedsReview(f"Shushing/initialism {candidate['surface']!r} requires an explicit context-chosen pronunciation span override")
    context = cue.get("context_review", {})
    if require_context:
        if context.get("status") != "CONFIRMED" or context.get("ambiguities"):
            raise NeedsReview("Cue context is ambiguous or has not been reviewed")
        inputs = context.get("reviewed_inputs", {})
        if (inputs.get("source_text_sha256") != text_hash(cue["source_text"]) or
                inputs.get("canonical_text_sha256") != text_hash(text) or
                inputs.get("glossary_version") != cue["glossary_version"]):
            raise NeedsReview("Context review does not match current source/text/glossary")
        if not isinstance(inputs.get("neighbor_cues"), list) or not inputs.get("character_context_version"):
            raise NeedsReview("Neighbor cue and character context evidence is required")
        if not context.get("reviewer"):
            raise NeedsReview("Context review requires reviewer provenance")
        analyses = context.get("analyses", [])
        if {a.get("category") for a in analyses} != set(CONTEXT_CATEGORIES) or len(analyses) != len(CONTEXT_CATEGORIES):
            raise NeedsReview("All context categories require an explicit review decision")
        for analysis in analyses:
            if analysis.get("status") not in {"CONFIRMED", "NOT_APPLICABLE"} or not analysis.get("evidence"):
                raise NeedsReview(f"Unresolved contextual category: {analysis.get('category')}")
            if analysis.get("status") == "CONFIRMED" and not analysis.get("interpretation"):
                raise NeedsReview("Confirmed contextual interpretation is missing")
        review_by_category = {a["category"]: a for a in analyses}
        for candidate in review_candidates(text):
            if review_by_category[candidate["category"]]["status"] != "CONFIRMED":
                raise NeedsReview(f"Candidate {candidate['surface']!r} needs explicit contextual interpretation")
        if cue.get("delivery_hint") and review_by_category["delivery"]["status"] != "CONFIRMED":
            raise NeedsReview("Delivery hint lacks contextual evidence")
    return {"technical_text": "PASS", "context": "RECORDED" if require_context else "NOT_CHECKED",
            "review_candidates": review_candidates(text), "semantic_approval": "NOT_PERFORMED"}


def _config(engine_config: dict) -> dict:
    config = {"voice": "af_heart", "language": "a", "device": "cpu", "precision": "float32",
              "speed": 1.0, "sample_rate": 48000, "channels": 1, "pcm_subtype": "PCM_16",
              "seed": 0, "num_threads": 4, "timeout_seconds": 120, **engine_config}
    for unsupported in ("emotion", "whisper", "pitch", "ssml", "delivery_prompt"):
        if unsupported in engine_config:
            raise ValueError(f"Kokoro adapter does not implement {unsupported} control")
    if config["speed"] != 1 or config["sample_rate"] != 48000 or config["pcm_subtype"] != "PCM_16" or config["channels"] != 1:
        raise ValueError("Fresh native synthesis requires speed 1, PCM_16 mono 48000 Hz")
    if config["language"] not in {"a", "b"} or not config["voice"].startswith(config["language"]):
        raise ValueError("This checked G2P alignment adapter supports matching English voices a/b only")
    if config["device"] not in {"cpu", "cuda"} or config["precision"] != "float32":
        raise ValueError("Use explicitly selected CPU/CUDA float32")
    if not config.get("run_id") or not isinstance(config.get("ffmpeg"), str):
        raise ValueError("run_id and explicit FFmpeg executable are required")
    if type(config["seed"]) is not int or type(config["num_threads"]) is not int or config["num_threads"] < 1:
        raise ValueError("Explicit integer seed and positive thread count are required")
    return config


def cache_identity(cue: dict, engine_config: dict, model_lock: dict) -> dict:
    config = _config(engine_config)
    # Include the full configuration, including even presently unsupported extras,
    # so no future audio-affecting parameter can accidentally reuse old audio.
    return {"adapter": ADAPTER_VERSION, "adapter_sha256": ADAPTER_SOURCE_SHA256,
            "run_id": config["run_id"], "cue_id": cue["cue_id"],
            "canonical_text": cue["canonical_text"], "pronunciation_overrides": cue.get("pronunciation_overrides", []),
            "pronunciation_rules_version": cue["pronunciation_rules_version"],
            "pronunciation_rules_sha256": config.get("pronunciation_rules_sha256"),
            "glossary_version": cue["glossary_version"], "engine_config": config,
            "model_lock": model_lock, "context_review": cue.get("context_review"),
            "delivery_hint": cue.get("delivery_hint"), "intent": cue.get("intent")}


def _align_tokens(text: str, tokens: list) -> list[dict]:
    """Exact character alignment; whitespace skipped only with an explicit record."""
    cursor, rows = 0, []
    for token in tokens:
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        surface = token.text
        if not surface or text[cursor:cursor + len(surface)] != surface:
            raise NeedsReview("G2P graphemes changed or lost source characters; alignment needs review")
        start = cursor
        cursor += len(surface)
        rows.append({"surface": surface, "span": [start, cursor], "phonemes": token.phonemes,
                     "whitespace": token.whitespace})
    if text[cursor:].strip():
        raise NeedsReview("G2P omitted trailing canonical characters")
    return rows


def prepare_engine_input(cue: dict, pipeline, model_vocab: dict) -> dict:
    validate_cue(cue)
    text = cue["canonical_text"]
    original_ps, original_tokens = pipeline.g2p(text)
    token_rows = _align_tokens(text, original_tokens)
    tokens = [deepcopy(t) for t in original_tokens]
    transformations = []
    for override in cue.get("pronunciation_overrides", []):
        start, end = override["span"]
        affected = [i for i, row in enumerate(token_rows)
                    if row["span"][0] < end and row["span"][1] > start]
        if not affected or token_rows[affected[0]]["span"][0] != start or token_rows[affected[-1]]["span"][1] != end:
            raise NeedsReview("Pronunciation span must cover whole aligned G2P tokens")
        for j, index in enumerate(affected):
            tokens[index].phonemes = override["phonemes"] if j == 0 else ""
        transformations.append({"span": [start, end], "surface": override["surface"],
                                "original_phonemes": [token_rows[i]["phonemes"] for i in affected],
                                "corrected_phonemes": override["phonemes"], "reason": override["reason"],
                                "evidence": override["evidence"], "type": override["type"]})
    # Unknown lexemes may be skipped by Kokoro/Misaki when fallback is absent.
    # Empty tokens are allowed only inside an explicitly overridden span or a
    # contiguous token group with a nonempty phoneme (Misaki's merged words).
    for index, token in enumerate(tokens):
        if token.phonemes is None:
            raise NeedsReview(f"G2P cannot pronounce token {token.text!r}")
        if not token.phonemes and any(c.isalnum() for c in token.text):
            covered = any(r["span"][0] <= token_rows[index]["span"][0] and
                          r["span"][1] >= token_rows[index]["span"][1] for r in cue.get("pronunciation_overrides", []))
            left = index
            while left > 0 and not tokens[left - 1].whitespace:
                left -= 1
            if not covered and not any(t.phonemes for t in tokens[left:index]):
                raise NeedsReview(f"G2P silently omitted word/number {token.text!r}")
    chunks, consumed = [], []
    for graphemes, phonemes, chunk_tokens in pipeline.en_tokenize(tokens):
        if not phonemes or len(phonemes) > 510:
            raise NeedsReview("Empty or overlength phoneme chunk; no truncation is permitted")
        unknown = sorted(set(phonemes) - set(model_vocab))
        if unknown:
            raise NeedsReview(f"Model would silently discard unsupported phoneme symbols: {unknown}")
        start_index = len(consumed)
        consumed.extend(chunk_tokens)
        expected = pipeline.tokens_to_ps(chunk_tokens)
        if phonemes != expected:
            raise ValueError("Chunk phonemes differ from checked token phonemes")
        rows = token_rows[start_index:len(consumed)]
        chunks.append({"index": len(chunks), "graphemes": graphemes, "phonemes": phonemes,
                       "span": [rows[0]["span"][0], rows[-1]["span"][1]],
                       "tokens": [{**row, "corrected_phonemes": token.phonemes}
                                  for row, token in zip(rows, chunk_tokens)]})
    if len(consumed) != len(tokens) or any(a is not b for a, b in zip(consumed, tokens)):
        raise ValueError("Engine tokenizer lost/reordered tokens")
    if not chunks:
        raise NeedsReview("No pronounceable content")
    # A compact whitespace comparison only verifies the chunk boundary trim;
    # _align_tokens above already checked every canonical non-space code point.
    actual = "".join(c["graphemes"] for c in chunks)
    if "".join(actual.split()) != "".join(text.split()):
        raise NeedsReview("Chunk graphemes do not cover canonical text")
    return {"mode": "kokoro_raw_phoneme_chunks", "canonical_text": text,
            "original_g2p": {"phonemes": original_ps, "tokens": token_rows},
            "corrected_phonemes": [c["phonemes"] for c in chunks], "chunks": chunks,
            "transformations": transformations, "coverage": "ALL_CANONICAL_CODEPOINTS_EXCEPT_RECORDED_WHITESPACE",
            "delivery_control": "unsupported" if cue.get("delivery_hint") else "not_requested"}


class KokoroAdapter:
    """Load only hash-locked local model files; no implicit HF model/voice fetch."""
    def __init__(self, config: dict, lock: dict):
        if lock.get("model") != MODEL_ID or not re.fullmatch(r"[0-9a-f]{40}", lock.get("revision", "")):
            raise ValueError("Exact model ID and immutable model revision are required")
        expected_version = lock.get("engine_version")
        if metadata.version("kokoro") != expected_version:
            raise ValueError("Installed Kokoro version differs from model lock")
        for package, version in lock.get("dependencies", {}).items():
            if metadata.version(package) != version:
                raise ValueError(f"Installed {package} differs from locked version")
        import torch
        from kokoro import KModel, KPipeline
        self.torch = torch
        if config["device"] == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA explicitly requested but unavailable")
        torch.manual_seed(config["seed"])
        torch.set_num_threads(config["num_threads"])
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(config["seed"])
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
        self.model = KModel(repo_id=MODEL_ID,
                            config=str(_checked_file(lock["files"]["config"], "model config")),
                            model=str(_checked_file(lock["files"]["weights"], "model weights")))
        self.model = self.model.to(config["device"]).float().eval()
        self.pipeline = KPipeline(lang_code=config["language"], repo_id=MODEL_ID, model=self.model)
        voice_row = lock.get("voices", {}).get(config["voice"])
        voice_path = _checked_file(voice_row, f"voice {config['voice']}")
        self.voice_pack = torch.load(str(voice_path), map_location="cpu", weights_only=True).to(self.model.device)
        self.vocab = self.model.vocab
        self.config = config
        self.source_evidence = {"files": [{"component": component, "path": inspect.getfile(value),
                                 "sha256": file_hash(Path(inspect.getfile(value)))}
                                for component, value in (("KModel", KModel), ("KPipeline", KPipeline),
                                                         ("G2P", type(self.pipeline.g2p)))],
                                "verified_controls": ["voice", "speed", "raw_phoneme_string"],
                                "unsupported_controls": engine_capabilities()["unsupported"]}

    def prepare(self, cue: dict) -> dict:
        return prepare_engine_input(cue, self.pipeline, self.vocab)

    def synthesize(self, engine_input: dict, raw_path: Path) -> dict:
        import numpy as np
        chunks, pcm = [], []
        for chunk in engine_input["chunks"]:
            outputs = list(self.pipeline.generate_from_tokens(chunk["phonemes"], voice=self.voice_pack, speed=1))
            if len(outputs) != 1 or outputs[0].phonemes != chunk["phonemes"] or outputs[0].audio is None:
                raise ValueError("Kokoro did not consume the complete checked phoneme chunk")
            array = outputs[0].audio.detach().cpu().numpy()
            if array.ndim != 1 or not len(array) or not np.isfinite(array).all():
                raise ValueError("Kokoro returned invalid audio")
            if float(np.max(np.abs(array))) > 1:
                raise NeedsReview("Native audio would clip at PCM conversion")
            pcm.append(array)
            chunks.append({"index": chunk["index"], "phonemes": outputs[0].phonemes,
                           "graphemes": chunk["graphemes"], "samples_24000": len(array)})
        audio = np.concatenate(pcm)
        samples = np.rint(audio * 32767).astype("<i2")
        with wave.open(str(raw_path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(samples.tobytes())
        return {"chunks": chunks, "native_samples": len(samples), "native_sample_rate": 24000,
                "actual_device": str(self.model.device), "actual_precision": str(next(self.model.parameters()).dtype),
                "engine_version": metadata.version("kokoro"), "speed": 1,
                "adapter_capability_evidence": self.source_evidence}


def _wave_info(path: Path, expected_rate: int) -> dict:
    with wave.open(str(path), "rb") as stream:
        if stream.getnchannels() != 1 or stream.getsampwidth() != 2 or stream.getframerate() != expected_rate or stream.getnframes() < 1:
            raise ValueError("Invalid synthesized PCM WAV format")
        samples = stream.getnframes()
        payload = stream.readframes(samples)
        if len(payload) != samples * 2:
            raise ValueError("Truncated PCM WAV payload")
        return {"samples": samples, "sample_rate": expected_rate, "channels": 1,
                "sample_width": 2, "duration_seconds": samples / expected_rate}


def synthesize_cue(cue: dict, output_dir: Path, engine_config: dict, model_lock: dict,
                   resume: bool = False, *, _adapter=None) -> dict:
    """Generate a new cue version, or verify reuse only inside this same run root.

    output_dir belongs to the caller's new run. A changed text/rule/voice yields a
    new directory; existing attempts are retained. No cross-run cache is read.
    """
    validate_cue(cue)
    config = _config(engine_config)
    rules_path = config.get("pronunciation_rules_path")
    if not rules_path or not config.get("pronunciation_rules_sha256"):
        raise ValueError("Versioned pronunciation rules path and hash are required")
    rules = read_json(_checked_file({"path": rules_path, "sha256": config["pronunciation_rules_sha256"]}, "pronunciation rules"))
    if rules.get("version") != cue["pronunciation_rules_version"]:
        raise ValueError("Pronunciation rule version does not match cue")
    for name in ("config", "weights"):
        _checked_file(model_lock.get("files", {}).get(name), f"model {name}")
    _checked_file(model_lock.get("voices", {}).get(config["voice"]), "selected voice")
    identity = cache_identity(cue, config, model_lock)
    key = object_hash(identity)
    root = guard_output(Path(output_dir), Path(output_dir), [])
    root.mkdir(parents=True, exist_ok=True)
    with process_lock(root / ".fresh-tts.lock"):
        owner_path = guard_output(root / "run-owner.json", root, [])
        owner = {"run_id": config["run_id"], "root": str(root)}
        if owner_path.exists() and read_json(owner_path) != owner:
            raise ValueError("TTS directory belongs to a different run/root")
        if not owner_path.exists():
            atomic_json(owner_path, owner)
        if resume:
            for attempt in sorted(root.glob(f"cue-{key}-v*"), reverse=True):
                receipt_path = guard_output(attempt / "receipt.json", root, [])
                if not receipt_path.is_file():
                    continue
                receipt = read_json(receipt_path)
                if receipt.get("identity") != identity or receipt.get("run_root") != str(root):
                    continue
                if (receipt.get("technical_status") != "PASS" or receipt.get("status") != "DRAFT" or
                        receipt.get("audio_path") != str(attempt / "audio-48000.wav") or
                        receipt.get("audio_sha256") != receipt.get("artifacts", {}).get("audio-48000.wav")):
                    raise ValueError("Invalid run-local TTS resume receipt fields")
                for relative, expected_hash in receipt.get("artifacts", {}).items():
                    artifact = guard_output(attempt / relative, root, [])
                    if not artifact.is_file() or file_hash(artifact) != expected_hash:
                        raise ValueError("Run-local TTS resume artifact is missing/corrupt")
                if set(receipt.get("artifacts", {})) != {"native-24000.wav", "audio-48000.wav", "engine-input.json", "engine-result.json", "resample-command.json"}:
                    raise ValueError("Incomplete run-local TTS resume receipt")
                _wave_info(attempt / "audio-48000.wav", 48000)
                return {**receipt, "receipt_path": str(receipt_path), "resume_verified": True}
        attempt = guard_output(root / f"cue-{key}-v{time.time_ns()}-{uuid.uuid4().hex[:8]}", root, [])
        attempt.mkdir(exist_ok=False)
        tick = time.perf_counter()
        try:
            adapter = _adapter or KokoroAdapter(config, model_lock)
            engine_input = adapter.prepare(cue)
            atomic_json(attempt / "engine-input.json", engine_input)
            raw = attempt / "native-24000.wav"
            engine_result = adapter.synthesize(engine_input, raw)
            atomic_json(attempt / "engine-result.json", engine_result)
            native = _wave_info(raw, 24000)
            output = attempt / "audio-48000.wav"
            command = [config["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                       "-i", str(raw), "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le",
                       "-map_metadata", "-1", str(output)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=config["timeout_seconds"], check=False)
            atomic_json(attempt / "resample-command.json", {"argv": command, "stdout": result.stdout,
                        "stderr": result.stderr, "returncode": result.returncode})
            if result.returncode:
                raise RuntimeError(f"FFmpeg resampling failed: {result.stderr}")
            info = _wave_info(output, 48000)
            if info["samples"] != native["samples"] * 2:
                raise ValueError("48 kHz sample count differs from exact 2x resampling")
            artifacts = {p.name: file_hash(p) for p in (raw, output, attempt / "engine-input.json",
                         attempt / "engine-result.json", attempt / "resample-command.json")}
            receipt = {"schema_version": 2, "status": "DRAFT", "technical_status": "PASS",
                       "pronunciation_review": "PENDING", "listening_review": "PENDING", "semantic_review": "PENDING",
                       "identity": identity, "cache_key": key, "run_root": str(root), "run_id": config["run_id"],
                       "cue": cue, "audio_path": str(output), "audio_sha256": artifacts[output.name],
                       "audio": info, "artifacts": artifacts, "wall_seconds": time.perf_counter() - tick,
                       "delivery_control": engine_input["delivery_control"], "capabilities": engine_capabilities(),
                       "fresh_synthesis": True, "resume_verified": False}
            atomic_json(attempt / "receipt.json", receipt)
            return {**receipt, "receipt_path": str(attempt / "receipt.json")}
        except Exception as exc:
            atomic_json(attempt / "failure.json", {"error_type": type(exc).__name__, "error": str(exc),
                        "identity": identity, "wall_seconds": time.perf_counter() - tick,
                        "status": "NEEDS_REVIEW" if isinstance(exc, NeedsReview) else "FAILED"})
            raise


def sample_voice(text: str, voice: str, output_dir: Path, engine_config: dict, model_lock: dict,
                 context_review: dict, glossary_version: str, pronunciation_rules_version: str) -> dict:
    """Audition uses the exact same checked path and remains DRAFT."""
    cue = {"cue_id": f"audition-{voice}", "source_text": text, "canonical_text": text,
           "glossary_version": glossary_version, "pronunciation_rules_version": pronunciation_rules_version,
           "context_review": context_review, "pronunciation_overrides": [], "intent": "audition", "delivery_hint": None}
    return synthesize_cue(cue, output_dir, {**engine_config, "voice": voice}, model_lock)
