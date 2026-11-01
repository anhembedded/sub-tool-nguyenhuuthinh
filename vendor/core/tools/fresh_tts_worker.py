"""Isolated Kokoro worker; run with the explicitly provisioned TTS interpreter."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--mode", choices=("synthesize", "voices", "capabilities"), default="synthesize")
    args = parser.parse_args(argv)
    from hongguo_ssot.fresh_tts import (KokoroAdapter, _config, engine_capabilities,
                                         list_voices, synthesize_cue)
    from hongguo_ssot.storage import atomic_json
    if args.response.exists():
        raise FileExistsError("Worker response exists; select a new version")
    request = json.loads(args.request.read_text(encoding="utf-8-sig"))
    tick = time.perf_counter()
    receipts = []
    try:
        if args.mode == "capabilities":
            response = {"capabilities": engine_capabilities()}
        elif args.mode == "voices":
            response = {"voices": list_voices(request["model_lock"], request.get("language"))}
        else:
            config = _config(request["engine_config"])
            # Redirect optional model/NLP caches to this new worker request root.
            # The adapter uses explicitly locked weight and voice files; these
            # variables only keep incidental dependency caches out of old runs.
            cache = Path(request.get("runtime_cache_dir", args.response.parent / "runtime-cache"))
            cache.mkdir(parents=True, exist_ok=True)
            os.environ["HF_HOME"] = str(cache / "huggingface")
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["HF_HUB_DISABLE_XET"] = "1"
            os.environ["TOKENIZERS_PARALLELISM"] = "false"
            os.environ["OMP_NUM_THREADS"] = str(config["num_threads"])
            os.environ["MKL_NUM_THREADS"] = str(config["num_threads"])
            # Additional explicit DLL directories are scoped to this worker;
            # never change system PATH, drivers, or an existing environment.
            handles = [os.add_dll_directory(p) for p in request.get("dll_directories", [])] if os.name == "nt" else []
            class LazyAdapter:
                value = None

                def __getattr__(self, name):
                    if self.value is None:
                        self.value = KokoroAdapter(config, request["model_lock"])
                    return getattr(self.value, name)

            adapter = LazyAdapter()
            for cue in request["cues"]:
                # Lazy create once; a verified resume does not require importing
                # the heavy engine. Subsequent fresh cues reuse one model.
                result = synthesize_cue(cue, Path(request["output_dir"]), config,
                                        request["model_lock"], request.get("resume", False), _adapter=adapter)
                receipts.append(result)
                print(json.dumps({"cue_id": cue["cue_id"], "status": result["technical_status"],
                                  "audio": result["audio_path"]}), flush=True)
            response = {"receipts": receipts, "status": "DRAFT", "technical_status": "PASS"}
        response["wall_seconds"] = time.perf_counter() - tick
        atomic_json(args.response, response)
        return 0
    except Exception as exc:
        atomic_json(args.response, {"receipts": receipts, "status": "FAILED", "error_type": type(exc).__name__,
                    "error": str(exc), "wall_seconds": time.perf_counter() - tick})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
