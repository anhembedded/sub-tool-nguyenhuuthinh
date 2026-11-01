"""Executable worker for the locked GPU engines. No CPU/model fallback."""
from __future__ import annotations

import argparse
import ctypes as C
import hashlib
import importlib.metadata as metadata
import json
import math
import os
import random
from pathlib import Path
import shutil
import sys
import time
import traceback

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hongguo_ssot.fresh_engines import (EngineError, check_hash, sha256,
                                      segments_from_frames, validate_roi, write_json)


class CUDAActivity:
    """Collect actual CUPTI concurrent kernel activity in the worker process.

    Raw activity buffers are retained; this proves executed GPU kernels, not
    GPU-only preprocessing or absence of every possible CPU operation.
    """

    def __init__(self, library: str, directory: Path):
        self.directory = directory
        directory.mkdir()
        self.lib = C.WinDLL(library) if os.name == "nt" else C.CDLL(library)
        self.buffers, self.records, self.errors = {}, [], []
        callback = C.WINFUNCTYPE if os.name == "nt" else C.CFUNCTYPE
        request_type = callback(None, C.POINTER(C.c_void_p), C.POINTER(C.c_size_t), C.POINTER(C.c_size_t))
        complete_type = callback(None, C.c_void_p, C.c_uint32, C.c_void_p, C.c_size_t, C.c_size_t)
        for name, args in [("cuptiActivityEnable", [C.c_uint32]),
                           ("cuptiActivityDisable", [C.c_uint32]),
                           ("cuptiActivityFlushAll", [C.c_uint32]),
                           ("cuptiActivityGetNextRecord", [C.c_void_p, C.c_size_t, C.POINTER(C.c_void_p)]),
                           ("cuptiActivityGetNumDroppedRecords", [C.c_void_p, C.c_uint32, C.POINTER(C.c_size_t)])]:
            fn = getattr(self.lib, name)
            fn.argtypes, fn.restype = args, C.c_int

        def request(ptr, size, maximum):
            buffer = C.create_string_buffer(2**21+8)
            address = (C.addressof(buffer)+7) & ~7
            self.buffers[address] = buffer
            ptr[0], size[0], maximum[0] = address, 2**21, 0

        def complete(context, stream, buffer, size, valid):
            try:
                if not valid:
                    return
                data = C.string_at(buffer, valid)
                path = self.directory / f"buffer-{len(self.records):05d}.bin"
                path.write_bytes(data)
                record, kinds = C.c_void_p(), {}
                while self.lib.cuptiActivityGetNextRecord(buffer, valid, C.byref(record)) == 0:
                    kind = C.c_uint32.from_address(record.value).value
                    kinds[kind] = kinds.get(kind, 0)+1
                dropped = C.c_size_t()
                self.lib.cuptiActivityGetNumDroppedRecords(context, stream, C.byref(dropped))
                self.records.append({"file": str(path), "sha256": sha256(path),
                                     "kinds": kinds, "dropped": dropped.value})
            except Exception as exc:
                self.errors.append(repr(exc))

        self.request, self.complete = request_type(request), complete_type(complete)
        register = self.lib.cuptiActivityRegisterCallbacks
        register.argtypes, register.restype = [request_type, complete_type], C.c_int
        if register(self.request, self.complete) != 0 or self.lib.cuptiActivityEnable(10) != 0:
            raise EngineError("CUPTI kernel activity unavailable; GPU execution not proven")

    def flush(self):
        if self.lib.cuptiActivityFlushAll(1) != 0:
            raise EngineError("CUPTI flush failed")

    def count(self):
        self.flush()
        return sum(x["kinds"].get(10, 0)+x["kinds"].get(3, 0) for x in self.records)

    def close(self):
        count = self.count()
        self.lib.cuptiActivityDisable(10)
        self.lib.cuptiFinalize()
        result = {"kernel_records": count, "records": self.records, "errors": self.errors}
        write_json(self.directory / "activity.json", result)
        if self.errors or not count or any(x["dropped"] for x in self.records):
            raise EngineError("Executed GPU kernel evidence missing/incomplete")
        return result


def provision(request: dict) -> tuple[dict, list]:
    cfg = request["config"]
    output = Path(request["output_dir"])
    lock = json.loads(Path(request["lock_path"]).read_text(encoding="utf-8"))
    section = lock[request["mode"]]
    if sys.version.split()[0] != section["python_version"]:
        raise EngineError("Engine Python version differs from lock")
    observed = {}
    for name, wanted in section["dependencies"].items():
        dist = metadata.distribution(name)
        if dist.version != wanted["version"]:
            raise EngineError(f"Locked dependency mismatch: {name} {dist.version}")
        record = dist.read_text("RECORD")
        digest = hashlib.sha256(record.encode()).hexdigest() if record else None
        if digest != wanted["record_sha256"]:
            raise EngineError(f"Installed dependency RECORD differs: {name}")
        observed[name] = {"version": dist.version, "record_sha256": digest}
    handles = []
    directories = [Path(x).resolve(strict=True) for x in cfg.get("dll_dirs", [])]
    for name, digest in section.get("dll_sha256", {}).items():
        matches = [x/name for x in directories if (x/name).is_file()]
        if not matches:
            raise EngineError(f"Required locked DLL missing: {name}")
        check_hash(matches[0], digest)
    # Environment changes below affect only this worker and its descendants.
    os.environ["PATH"] = os.pathsep.join(map(str, directories))+os.pathsep+os.environ.get("PATH", "")
    if os.name == "nt":
        handles = [os.add_dll_directory(str(x)) for x in directories]
    cupti = Path(cfg["cupti_library"]).resolve(strict=True)
    check_hash(cupti, lock["cupti_sha256"])
    os.environ.update(PADDLE_PDX_CACHE_HOME=str(output/"paddlex-cache"),
                      PADDLE_HOME=str(output/"paddle-cache"),
                      PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True",
                      OMP_NUM_THREADS="4", MKL_NUM_THREADS="4")
    write_json(output/"environment.json", {"python": sys.version, "dependencies": observed,
               "dll_directories": list(map(str, directories)), "cupti_sha256": sha256(cupti),
               "lock_sha256": sha256(request["lock_path"])})
    return lock, handles


def json_value(value):
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def run_ocr(request: dict, lock: dict) -> dict:
    import cv2
    import numpy as np
    import paddle
    from paddleocr import PaddleOCR
    from paddlex.inference.models.common.static_infer import PaddleInfer

    cfg, output = request["config"], Path(request["output_dir"])
    if not paddle.is_compiled_with_cuda() or paddle.device.cuda.device_count() < 1:
        raise EngineError("Paddle CUDA GPU unavailable; CPU fallback forbidden")
    paddle.set_device("gpu:0")
    local_models = {}
    for key, name in [("detector_dir", "PP-OCRv5_mobile_det"), ("recognizer_dir", "PP-OCRv5_mobile_rec")]:
        origin = Path(cfg[key]).resolve(strict=True)
        target = output/"models"/name
        target.mkdir(parents=True)
        for filename, digest in lock["ocr"]["models"][name]["files"].items():
            check_hash(origin/filename, digest)
            shutil.copyfile(origin/filename, target/filename)
        local_models[key] = str(target)
    reader = PaddleOCR(text_detection_model_name="PP-OCRv5_mobile_det",
                       text_recognition_model_name="PP-OCRv5_mobile_rec",
                       text_detection_model_dir=local_models["detector_dir"],
                       text_recognition_model_dir=local_models["recognizer_dir"],
                       use_doc_orientation_classify=False, use_doc_unwarping=False,
                       use_textline_orientation=False, text_rec_score_thresh=0,
                       device="gpu:0", enable_hpi=False, use_tensorrt=False,
                       precision="fp32", enable_cinn=False)
    activity = CUDAActivity(cfg["cupti_library"], output/"gpu-activity")
    calls = []
    original = PaddleInfer.__call__

    def traced(self, x):
        tensors = x
        before = activity.count()
        types = [str(x.dtype) for x in tensors]
        if self._option.device_type != "gpu" or self._option.run_mode != "paddle":
            raise EngineError("Paddle engine changed from GPU FP32 native backend")
        if any(x != "float32" for x in types):
            raise EngineError("Paddle input tensor is not FP32")
        result = original(self, tensors)
        paddle.device.synchronize()
        count = activity.count()-before
        calls.append({"model": self._model_name, "device": self._option.device_type,
                      "run_mode": self._option.run_mode, "input_dtypes": types,
                      "input_shapes": [list(x.shape) for x in tensors], "kernel_records": count})
        if not count:
            raise EngineError("Paddle model did not execute GPU kernels")
        return result

    PaddleInfer.__call__ = traced
    cap = cv2.VideoCapture(request["source"])
    if not cap.isOpened():
        raise EngineError("Source video cannot be decoded")
    width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    maximum = cfg.get("max_frames", total)
    if type(maximum) is not int or not 0 < maximum <= total:
        raise EngineError("Explicit selected frame count outside source")
    x, y, w, h = validate_roi(request["roi"], request["film_id"], request["source_sha256"], width, height, maximum)
    write_json(output/"roi.json", request["roi"])
    roi_evidence = {item["frame_index"]: item for item in request["roi"]["verification"]["evidence"]}
    checked_evidence = []
    for folder in ["native-roi", "native-boundaries", "detector-input"]:
        (output/folder).mkdir()
    seen, rows, previous, previous_frame, active_text = {}, [], None, None, None
    raw = (output/"raw.jsonl").open("x", encoding="utf-8")
    frame_log = (output/"frames.jsonl").open("x", encoding="utf-8")
    gpu_proof = None
    try:
        for index in range(maximum):
            ok, image = cap.read()
            if not ok or image.shape != (height, width, 3):
                raise EngineError(f"Native frame decode incomplete at {index}")
            if index in roi_evidence:
                proof_image = cv2.imread(roi_evidence[index]["path"], cv2.IMREAD_COLOR)
                if proof_image is None or not np.array_equal(proof_image, image):
                    raise EngineError("ROI evidence image is not the actual full native source frame")
                checked_evidence.append(index)
            crop = image[y:y+h, x:x+w].copy()
            pixels = crop.tobytes()
            digest = hashlib.sha256(pixels).hexdigest()
            reuse = digest in seen
            if reuse:
                entry = seen[digest]
                # Verify bytes from saved lossless PNG, not confidence/mask/fuzzy similarity.
                stored = cv2.imread(entry["native_roi_path"], cv2.IMREAD_COLOR)
                if stored is None or stored.tobytes() != pixels:
                    raise EngineError("Exact native-pixel reuse proof failed")
            else:
                path = output/"native-roi"/f"f{index:09d}.png"
                if not cv2.imwrite(str(path), crop):
                    raise EngineError("Cannot preserve native ROI")
                # Only pad to the detector stride, keeping every input pixel intact.
                padded = cv2.copyMakeBorder(crop, 0, (-h)%32, 0, (-w)%32, cv2.BORDER_CONSTANT)
                if max(padded.shape[:2]) > 4000:
                    raise EngineError("Native ROI exceeds locked Paddle detector max side; no automatic downscale")
                input_path = output/"detector-input"/path.name
                if not cv2.imwrite(str(input_path), padded):
                    raise EngineError("Cannot preserve detector input")
                results = list(reader.predict_iter(padded, text_det_limit_type="max",
                               text_det_limit_side_len=max(padded.shape[:2])))
                paddle.device.synchronize()
                if len(results) != 1:
                    raise EngineError("Expected exactly one OCR result per native image")
                # Official serializable raw export excludes visualization Font
                # objects and redundant image arrays, preserved separately above.
                result = results[0].json["res"]
                text = "\n".join(result.get("rec_texts", []))
                entry = {"call_id": len(seen)+1, "frame_index": index,
                         "native_roi_path": str(path), "native_roi_sha256": sha256(path),
                         "native_pixel_sha256": digest, "native_shape": list(crop.shape),
                         "detector_input_path": str(input_path), "detector_input_sha256": sha256(input_path),
                         "detector_input_shape": list(padded.shape), "text": text,
                         "raw_output": result, "rec_scores": list(result.get("rec_scores", []))}
                raw.write(json.dumps(entry, ensure_ascii=False, default=json_value)+"\n")
                raw.flush()
                seen[digest] = entry
                proven = {c["model"] for c in calls if c["kernel_records"] > 0}
                if gpu_proof is None and proven == {"PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec"}:
                    PaddleInfer.__call__ = original
                    gpu_proof = activity.close()
            text = entry["text"]
            if text != active_text:
                if previous_frame is not None:
                    cv2.imwrite(str(output/"native-boundaries"/f"f{index-1:09d}.png"), previous_frame)
                cv2.imwrite(str(output/"native-boundaries"/f"f{index:09d}.png"), image)
                active_text = text
            row = {"frame_index": index, "native_frame_pixel_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                   "roi_pixel_sha256": digest, "raw_call_id": entry["call_id"],
                   "reused_exact_pixels": reuse, "reused_from_frame": entry["frame_index"] if reuse else None,
                   "text": text, "confidence": entry["rec_scores"]}
            frame_log.write(json.dumps(row, ensure_ascii=False, default=json_value)+"\n")
            frame_log.flush()
            rows.append(row)
            previous_frame = image
            if index % 100 == 0:
                print(f"OCR native frame {index}/{maximum}", flush=True)
        if previous_frame is not None:
            cv2.imwrite(str(output/"native-boundaries"/f"f{maximum-1:09d}.png"), previous_frame)
    finally:
        cap.release()
        raw.close()
        frame_log.close()
        PaddleInfer.__call__ = original
    if gpu_proof is None:
        activity.close()
        raise EngineError("Detector and recognizer GPU execution could not both be proven")
    segments = segments_from_frames(rows, request["source_sha256"][:16])
    write_json(output/"chinese_ocr.json", {"schema_version": 2, "segments": segments,
               "complete": True, "source_sha256": request["source_sha256"],
               "selected_frames": maximum, "source_total_frames": total,
               "raw_output_path": str(output/"raw.jsonl"), "frame_lineage_path": str(output/"frames.jsonl")})
    return {"technical_status": "PASS", "content_review": "NEEDS_REVIEW", "status": "DRAFT",
            "engine": "PaddleOCR", "precision": "FP32", "device": "gpu:0",
            "selected_frames": maximum, "native_frame_density": "every selected native frame",
            "source_total_frames": total, "unique_pixel_inferences": len(seen),
            "exact_pixel_reuse_frames": maximum-len(seen), "cue_count": len(segments),
            "source_sha256": request["source_sha256"], "roi": request["roi"],
            "roi_evidence_pixel_verified_frames": checked_evidence,
            "output": str(output/"chinese_ocr.json"), "gpu_calls": calls,
            "gpu_activity": gpu_proof, "raw_output_sha256": sha256(output/"raw.jsonl"),
            "boundary_policy": "Every OCR text transition, prior frame and first/last native frames preserved",
            "limits": ["Native detector input padded to stride32; standard model recognition crop normalization remains",
                       "Confidence does not certify text accuracy; no corrections applied",
                       "ROI evidence requires per-film visual verification; outside-ROI content is not inferred"]}


def run_separation(request: dict, lock: dict) -> dict:
    import numpy as np
    import soundfile as sf
    import torch
    from scipy.signal import correlate, correlation_lags, resample_poly
    from demucs.states import load_model
    from demucs.apply import apply_model

    cfg, output = request["config"], Path(request["output_dir"])
    checkpoint = Path(cfg["checkpoint"]).resolve(strict=True)
    check_hash(checkpoint, lock["separate"]["model"]["sha256"])
    if checkpoint.name != "97d170e1-dbb4db15.th":
        raise EngineError("MVSEP checkpoint filename differs from required artifact")
    if not torch.cuda.is_available():
        raise EngineError("MVSEP CUDA unavailable; CPU fallback forbidden")
    torch.set_num_threads(4)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    seed = int(cfg.get("seed", 20261022))
    torch.manual_seed(seed)
    np.random.seed(seed % 2**32)
    random.seed(seed)
    # Deserialization only after exact trusted official artifact hash validation.
    model = load_model(checkpoint).eval().float().cuda()
    if list(model.sources) != ["music", "sfx", "speech"] or model.samplerate != 44100:
        raise EngineError("MVSEP stem identity/sample rate mismatch")
    if any(p.dtype != torch.float32 or not p.is_cuda for p in model.parameters()):
        raise EngineError("MVSEP model is not GPU float32")
    audio, sr = sf.read(request["source"], dtype="float32", always_2d=True)
    if sr != 48000 or audio.shape[1] != 2 or not len(audio) or not np.isfinite(audio).all():
        raise EngineError("Separation input must be finite native-timeline PCM 48000Hz stereo")
    sampled = resample_poly(audio, 147, 160, axis=0).astype("float32")
    tensor = torch.from_numpy(sampled.T.copy()).unsqueeze(0).cuda()
    activity = CUDAActivity(cfg["cupti_library"], output/"gpu-activity")
    torch.cuda.reset_peak_memory_stats()
    with torch.inference_mode():
        stems = apply_model(model, tensor, shifts=1, overlap=0.8, device="cuda", progress=False)
        torch.cuda.synchronize()
    gpu_proof = activity.close()
    if stems.dtype != torch.float32 or not stems.is_cuda or tuple(stems.shape) != (1, 3, 2, len(sampled)):
        raise EngineError("MVSEP output dtype/device/length mismatch")
    values = stems[0].cpu().numpy()
    if not np.isfinite(values).all():
        raise EngineError("Nonfinite separated audio")
    paths = {}
    for index, name in enumerate(model.sources):
        path = output/f"{name}-44100.wav"
        sf.write(path, values[index].T, 44100, subtype="FLOAT")
        paths[name] = {"path": str(path), "sha256": sha256(path)}
    background_44100 = (values[0]+values[1]).T
    background = resample_poly(background_44100, 160, 147, axis=0).astype("float32")
    rounding = len(background)-len(audio)
    if not 0 <= rounding <= 1:
        raise EngineError("Unexpected resampling timeline length")
    background = background[:len(audio)]
    raw_path = output/"music_effects-float48.wav"
    sf.write(raw_path, background, 48000, subtype="FLOAT")
    clipped_samples = int(np.count_nonzero((background < -1.0) | (background > 32767/32768)))
    write_json(output/"pcm-conversion.json", {"raw_float_path": str(raw_path),
               "raw_float_sha256": sha256(raw_path), "output_subtype": "PCM_16",
               "out_of_pcm16_range_samples": clipped_samples,
               "peak_absolute": float(np.max(np.abs(background))),
               "gain_changed": False, "clipping_applied": False})
    if clipped_samples:
        raise EngineError("Separated background exceeds PCM16 range; raw float retained, no silent clipping/gain change")
    path = output/"music_effects.wav"
    sf.write(path, background, 48000, subtype="PCM_16")
    checked, final_sr = sf.read(path, dtype="float32", always_2d=True)
    if final_sr != 48000 or checked.shape != audio.shape or not np.isfinite(checked).all():
        raise EngineError("Final separated background PCM integrity failed")
    # 10ms summed-stem energy-envelope correlation is only an alignment proxy.
    size = len(sampled)//441*441
    a = np.abs(sampled[:size]).mean(axis=1).reshape(-1, 441).mean(axis=1)
    b = np.abs(values.sum(axis=0).T[:size]).mean(axis=1).reshape(-1, 441).mean(axis=1)
    if len(a) > 1 and a.std() > 1e-12 and b.std() > 1e-12:
        corr = correlate(b-b.mean(), a-a.mean(), mode="full", method="fft")
        lags = correlation_lags(len(b), len(a))
        valid = np.abs(lags) <= 50
        lag = int(lags[valid][np.argmax(corr[valid])])*10
    else:
        lag = None
    residual = values.sum(axis=0).T-sampled
    reconstruction_rms = float(np.sqrt(np.mean(residual.astype("float64")**2)))
    write_json(output/"review.json", {"speech_leakage": "NEEDS_LISTENING_REVIEW",
               "lost_effects": "NEEDS_LISTENING_REVIEW", "individual_onset_alignment": "NEEDS_REVIEW",
               "summed_stem_envelope_lag_ms": lag, "proxy_resolution_ms": 10,
               "reconstruction_residual_rms": reconstruction_rms,
               "notes": "No claim of listening, clean separation, or semantic approval"})
    return {"technical_status": "PASS", "status": "DRAFT", "engine": "MVSEP-CDX23",
            "precision": "FP32", "device": torch.cuda.get_device_name(0),
            "model_sha256": sha256(checkpoint), "shifts": 1, "overlap": 0.8,
            "seed": seed, "seed_scope": "Python, NumPy and Torch; bitwise GPU determinism not guaranteed",
            "sources": list(model.sources), "included_stems": ["music", "sfx"], "excluded_stems": ["speech"],
            "input_samples": len(audio), "output_samples": len(background), "sample_rate": 48000,
            "model_input_samples": len(sampled), "model_output_samples": values.shape[-1],
            "resampling_rounding_samples_removed": rounding, "output": str(path),
            "output_sha256": sha256(path), "raw_float_path": str(raw_path),
            "raw_float_sha256": sha256(raw_path), "output_subtype": "PCM_16",
            "pcm16_clipped_samples": clipped_samples, "stems": paths, "gpu_activity": gpu_proof,
            "peak_vram_allocated": torch.cuda.max_memory_allocated(),
            "summed_stem_envelope_lag_ms": lag, "alignment_proxy_status": "PASS" if lag == 0 else "NEEDS_REVIEW",
            "speech_leakage_review": "NEEDS_REVIEW", "lost_effects_review": "NEEDS_REVIEW",
            "listening_approval": "NOT_PERFORMED", "source_sha256": request["source_sha256"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    output, began = Path(request["output_dir"]), time.monotonic()
    try:
        check_hash(request["source"], request["source_sha256"])
        lock, handles = provision(request)
        functions = {"ocr": run_ocr, "separate": run_separation}
        receipt = functions[request["mode"]](request, lock)
        check_hash(request["source"], request["source_sha256"])
        receipt.update(worker_sha256=sha256(__file__), lock_sha256=sha256(request["lock_path"]),
                       wall_seconds=time.monotonic()-began)
        write_json(output/"receipt.json", receipt)
        print(json.dumps({"technical_status": receipt["technical_status"], "output": receipt["output"]}), flush=True)
        return 0
    except Exception as exc:
        write_json(output/"failure.json", {"technical_status": "FAIL", "type": type(exc).__name__,
                   "error": str(exc), "traceback": traceback.format_exc(), "wall_seconds": time.monotonic()-began})
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
