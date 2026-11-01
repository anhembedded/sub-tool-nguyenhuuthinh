"""Final-output metadata policy and an auditable, unmodified ffprobe receipt."""
from pathlib import Path
import json
import subprocess

from .storage import atomic_json


FINAL_METADATA_OPTIONS = ("-map_metadata", "-1", "-map_metadata:s", "-1",
                          "-map_chapters", "-1")


def audit_output_metadata(output: Path, receipt: Path, *, ffprobe="ffprobe",
                          render_command=None) -> dict:
    """Inspect tags, not codec/timing; technical/semantic QA remains separate.

    Disabled automatic inheritance does not prohibit muxer/encoder-generated
    tags, container identifiers or explicitly authored metadata. Tag provenance
    cannot be inferred from tag names alone; retain the actual command too.
    """
    command = [str(ffprobe), "-v", "error", "-show_entries",
               "format_tags:stream_tags:chapters", "-of", "json", str(output)]
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False)
    record = {"output": str(output), "command": command,
              "render_command": render_command, "returncode": result.returncode,
              "stdout": result.stdout, "stderr": result.stderr,
              "automatic_inheritance_disabled": ["global_tags", "stream_tags", "chapters"],
              "interpretation": "Encoder/muxer-generated tags may remain. This is not a metadata-free claim; tag origins require comparison with inputs and the executed command."}
    try:
        record["probe"] = json.loads(result.stdout) if result.returncode == 0 else None
    finally:
        atomic_json(receipt, record)
    if result.returncode:
        raise RuntimeError(f"Metadata ffprobe failed; see {receipt}")
    if record["probe"].get("chapters"):
        raise ValueError(f"Unexpected final chapters; see {receipt}")
    return record
