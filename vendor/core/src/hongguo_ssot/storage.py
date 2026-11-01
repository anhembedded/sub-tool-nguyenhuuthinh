"""Atomic manifests, content fingerprints, protected paths, and process locks."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import time
import uuid


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def file_hash(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def object_hash(value) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def inside(path: Path, parent: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def guard_output(path: Path, allowed_root: Path, protected_roots: list[Path]) -> Path:
    # Check the lexical path before resolve() erases evidence of a junction.
    for component in [Path(path), *Path(path).parents]:
        if component.exists() or component.is_symlink():
            info = component.lstat()
            if component.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Output path contains a symlink/junction: {component}")
    target = Path(path).resolve()
    if not inside(target, allowed_root):
        raise ValueError(f"Output escapes this run/cache root: {target}")
    for protected in protected_roots:
        if inside(target, protected):
            raise ValueError(f"Refusing a write under protected input/output: {target}")
    return target


def validate_output_tree(root: Path, protected_roots: list[Path]) -> None:
    """Reject pre-existing file links and junctions without following them.

    Run/cache contents are not trusted merely because their top-level path is
    safe. Stage implementations may open nested filenames internally.
    """
    root = guard_output(root, root, protected_roots)
    if not root.exists():
        return
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError(f"Writable tree contains a symlink/junction: {entry.path}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))


@contextmanager
def process_lock(path: Path):
    """Hold a nonblocking OS lock; the kernel releases it after process death.

    Keep the lock file permanently: unlink/recreate would permit two processes
    to lock different inodes/handles for the same path. Metadata describes the
    last successful owner only and never determines ownership or recovery.
    Byte zero is reserved for the Windows CRT range lock; a leading newline
    also leaves the metadata valid JSON for diagnostics.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        # Byte ranges may extend beyond EOF; do not write even the sentinel
        # before acquisition, since another opener may already hold that byte.
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Pipeline/cache is locked or could not be locked: {path}") from exc
        try:
            metadata = {"pid": os.getpid(), "host": socket.gethostname(),
                        "token": uuid.uuid4().hex, "created_epoch": time.time(),
                        "mechanism": "os-advisory-lock", "metadata_is_ownership_proof": False}
            stream.seek(0)
            stream.write(b"\n")
            stream.write((json.dumps(metadata) + "\n").encode("utf-8"))
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class StageState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = read_json(path) if self.path.exists() else {"schema_version": 1, "stages": {}}
        if self.data.get("schema_version") != 1:
            raise ValueError("Unsupported stage-state schema")

    def reusable(self, key: str, signature: str) -> bool:
        row = self.data["stages"].get(key, {})
        return (row.get("status") == "PASS" and row.get("signature") == signature
                and bool(row.get("artifacts"))
                and all(Path(p).is_file() and file_hash(Path(p)) == h
                        for p, h in row["artifacts"].items()))

    def save(self, key: str, signature: str, artifacts: list[Path], **details) -> None:
        self.data["stages"][key] = {"status": "PASS", "signature": signature,
                                    "artifacts": {str(p.resolve()): file_hash(p) for p in artifacts},
                                    "details": details}
        atomic_json(self.path, self.data)

    def fail(self, key: str, signature: str, error: Exception) -> None:
        self.data["stages"][key] = {"status": "FAILED", "signature": signature,
                                    "error": f"{type(error).__name__}: {error}"}
        atomic_json(self.path, self.data)
