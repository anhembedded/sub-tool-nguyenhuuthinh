"""V3 MVSEP worker: retain finite IEEE float32 stems until the final mix.

The unchanged v2 worker supplies exact environment, dependency, DLL and CUPTI
validation. Model inference and alignment checks retain its GPU FP32 contract.
No clipping, normalization or intermediate gain is applied to float headroom.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import random
import sys
import time
import traceback

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hongguo_ssot.fresh_engines import EngineError, check_hash, sha256, write_json
from fresh_engine_worker import CUDAActivity, provision


def preserve_float_background(background, output, expected_shape, name='background-float48.wav'):
    """Write and verify every float32 sample, including finite values above 1."""
    import numpy as np
    import soundfile as sf
    if background.dtype != np.float32 or background.shape != expected_shape or not np.isfinite(background).all():
        raise EngineError("Invalid float32 background samples")
    path = Path(output)/name
    sf.write(path, background, 48000, subtype="FLOAT")
    checked, rate = sf.read(path, dtype="float32", always_2d=True)
    if (rate != 48000 or checked.shape != background.shape or not np.isfinite(checked).all()
            or checked.tobytes() != background.tobytes()):
        raise EngineError("Float background preservation failed")
    proof = {"path": str(path), "sha256": sha256(path), "output_subtype": "FLOAT",
             "sample_rate": rate, "channels": checked.shape[1], "samples": len(checked),
             "out_of_pcm16_range_samples": int(np.count_nonzero((background < -1.0) | (background > 32767/32768))),
             "peak_absolute": float(np.max(np.abs(background))),
             "exact_float32_samples_preserved": True, "gain_changed": False,
             "clipping_applied": False, "normalization_applied": False,
             "gain_policy": "Retain IEEE float32 headroom until configured final background gain and limiter"}
    write_json(Path(output)/"float-preservation.json", proof)
    return path, proof


def selected_background(values, included):
    """Select stems before resampling; never silently fall back to a sum."""
    if included == ['sfx']:
        return values[1].T
    if included == ['music', 'sfx']:
        return (values[0]+values[1]).T
    raise EngineError('Select explicit sfx or music+sfx; source speech is forbidden')


def run_separation(request: dict, lock: dict) -> dict:
    import numpy as np
    import soundfile as sf
    import torch
    from scipy.signal import correlate, correlation_lags, resample_poly
    from demucs.states import load_model
    from demucs.apply import apply_model

    cfg, output = request["config"], Path(request["output_dir"])
    included = cfg.get('included_stems')
    if included not in [['sfx'], ['music', 'sfx']]:
        raise EngineError('Select explicit sfx or music+sfx; source speech is forbidden')
    if cfg.get('precision')!='FP32' or cfg.get('shifts')!=1 or cfg.get('overlap')!=.8:
        raise EngineError('Supported separation policy requires FP32, shifts=1, overlap=0.8')
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
    background_44100 = selected_background(values, included)
    background = resample_poly(background_44100, 160, 147, axis=0).astype("float32")
    rounding = len(background)-len(audio)
    if not 0 <= rounding <= 1:
        raise EngineError("Unexpected resampling timeline length")
    background = background[:len(audio)]
    path, preservation = preserve_float_background(background, output, audio.shape,
                        'sfx-float48.wav' if included == ['sfx'] else 'music-sfx-float48.wav')
    raw_path = path
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
            "sources": list(model.sources), "included_stems": included,
            "excluded_stems": [s for s in model.sources if s not in included],
            "input_samples": len(audio), "output_samples": len(background), "sample_rate": 48000,
            "model_input_samples": len(sampled), "model_output_samples": values.shape[-1],
            "resampling_rounding_samples_removed": rounding, "output": str(path),
            "output_sha256": sha256(path), "raw_float_path": str(raw_path),
            "raw_float_sha256": sha256(raw_path), "output_subtype": "FLOAT",
            "float_preservation": preservation, "stems": paths, "gpu_activity": gpu_proof,
            "peak_vram_allocated": torch.cuda.max_memory_allocated(),
            "summed_stem_envelope_lag_ms": lag, "alignment_proxy_status": "PASS" if lag == 0 else "NEEDS_REVIEW",
            "speech_leakage_review": "NEEDS_REVIEW", "lost_effects_review": "NEEDS_REVIEW",
            "listening_approval": "NOT_PERFORMED", "source_sha256": request["source_sha256"]}



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    output, began = Path(request["output_dir"]), time.monotonic()
    try:
        if request["mode"] != "separate": raise EngineError("V3 separation worker requires separate mode")
        check_hash(request["source"], request["source_sha256"])
        lock, handles = provision(request)
        receipt = run_separation(request, lock)
        check_hash(request["source"], request["source_sha256"])
        receipt.update(worker_sha256=sha256(__file__),
                       validation_worker_sha256=sha256(Path(__file__).with_name("fresh_engine_worker.py")),
                       lock_sha256=sha256(request["lock_path"]), wall_seconds=time.monotonic()-began)
        write_json(output/"receipt.json", receipt)
        write_json(args.response, receipt)
        print(json.dumps({"technical_status": receipt["technical_status"], "output": receipt["output"]}), flush=True)
        return 0
    except Exception as exc:
        failure = {"technical_status": "FAIL", "type": type(exc).__name__, "error": str(exc),
                   "traceback": traceback.format_exc(), "wall_seconds": time.monotonic()-began}
        write_json(output/"failure.json", failure)
        write_json(args.response, failure)
        traceback.print_exc()
        return 1


if __name__ == "__main__": raise SystemExit(main())
