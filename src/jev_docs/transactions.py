"""Serialize synchronizers and roll validated generated batches forward after interruption."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

PENDING = ".jev-docs-pending"
STATIC_PATHS = frozenset(
    {
        "sources/docs.typesafe.ai/manifest.json",
        "sources/github.typesafe-ai/manifest.json",
        "BEST_PRACTICES.md",
        "MODEL_LIMITATIONS.md",
        "events/baseline.json",
        *(
            f"state/{name}.json"
            for name in (
                "sources",
                "source-coverage",
                "freshness",
                "primitives",
                "api",
                "models",
                "practices",
                "sdk-python",
                "sdk-javascript",
                "skill",
                "model-limitations",
                "cookbooks",
                "evaluation-tool",
            )
        ),
    }
)


@contextmanager
def synchronization_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".jev-docs.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another synchronization owns this generated tree") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _allowed_path(value: str) -> None:
    if isinstance(value, str):
        if value in STATIC_PATHS:
            return
        match = re.fullmatch(r"(events|changes)/(\d{4}-\d{2}-\d{2})\.(json|md)", value)
        if match and (match[1], match[3]) in {("events", "json"), ("changes", "md")}:
            try:
                date.fromisoformat(match[2])
                return
            except ValueError:
                pass
    raise RuntimeError("unauthorized promotion path in pending synchronization")


def _plain_path(base: Path, relative: str) -> Path:
    path = base
    if path.is_symlink():
        raise RuntimeError("symlink in pending synchronization path")
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise RuntimeError("symlink in pending synchronization path")
    return path


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_durable(path: Path, data: bytes) -> None:
    with path.open("wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _promotion_order(relative: str) -> tuple[int, str]:
    if relative == "events/baseline.json":
        return (3, relative)
    if relative.startswith("changes/"):
        return (0, relative)
    if relative.startswith("events/"):
        return (1, relative)
    return (2, relative)


def recover_pending(root: Path) -> None:
    """Roll forward the original batch, never rederive it from a later observation.

    The caller holds synchronization_lock. Candidate files remain immutable until
    all targets are durable; READY is then durably removed before journal cleanup.
    """
    pending = _plain_path(root, PENDING)
    if not pending.exists():
        return
    ready = _plain_path(pending, "READY")
    if not ready.exists():
        # Preparation never replaces targets before READY. Completed batches also
        # clear READY before cleanup, making interrupted cleanup safe to resume.
        shutil.rmtree(pending)
        _sync_directory(root)
        return
    manifest_path = _plain_path(pending, "manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError("invalid pending synchronization manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise RuntimeError("invalid pending synchronization manifest")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("invalid pending synchronization file list")
    paths = []
    for entry in files:
        if not isinstance(entry, dict):
            raise RuntimeError("invalid pending synchronization file record")
        relative = entry.get("path")
        _allowed_path(relative)
        paths.append(relative)
    if len(set(paths)) != len(paths):
        raise RuntimeError("duplicate pending synchronization paths")
    # Validate the entire batch before replacing even one target.
    for entry in files:
        source = _plain_path(pending / "files", entry["path"])
        _plain_path(root, entry["path"])
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != entry.get(
            "sha256"
        ):
            raise RuntimeError("pending synchronization candidate checksum mismatch")
    parents = {root}
    replacement = _plain_path(pending, "replacement")
    for relative in sorted(paths, key=_promotion_order):
        source = pending / "files" / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_durable(replacement, source.read_bytes())
        os.replace(replacement, target)
        parent = target.parent
        while parent != root:
            parents.add(parent)
            parent = parent.parent
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        _sync_directory(parent)
    ready.unlink()
    _sync_directory(pending)
    shutil.rmtree(pending)
    _sync_directory(root)


def promote(stage: Path, root: Path) -> None:
    """Publish a validated stage with a durable, allowlisted roll-forward journal."""
    paths = sorted(
        path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()
    )
    for relative in paths:
        _allowed_path(relative)
        _plain_path(stage, relative)
        _plain_path(root, relative)
    pending = _plain_path(root, PENDING)
    if pending.exists():
        raise RuntimeError("pending synchronization must be recovered before promotion")
    pending.mkdir()
    files = []
    for relative in paths:
        target = pending / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = (stage / relative).read_bytes()
        _write_durable(target, data)
        files.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest()})
    _write_durable(
        pending / "manifest.json",
        (json.dumps({"version": 1, "files": files}, sort_keys=True) + "\n").encode(),
    )
    directories = [path for path in pending.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _sync_directory(directory)
    _sync_directory(pending)
    _sync_directory(root)
    _write_durable(pending / "READY", b"")
    _sync_directory(pending)
    recover_pending(root)
