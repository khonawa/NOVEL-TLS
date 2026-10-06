from __future__ import annotations

import json
from pathlib import Path


def checkpoint_dir_for(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".progress")

def load_checkpoint(progress_dir: Path, fingerprint: str, chunk_count: int) -> dict[int, str]:
    manifest_path = progress_dir / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            return {}
        # Field-by-field comparison (not whole-dict equality) so that adding
        # new manifest fields in the future does not invalidate checkpoints.
        if (
            manifest.get("version") != 1
            or manifest.get("fingerprint") != fingerprint
            or manifest.get("chunk_count") != chunk_count
        ):
            return {}
        completed: dict[int, str] = {}
        for path in progress_dir.glob("chunk_*.txt"):
            stem = path.stem.removeprefix("chunk_")
            if not stem.isdigit():
                continue
            index = int(stem)
            if 1 <= index <= chunk_count:
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    completed[index] = text
        return completed
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}

def initialize_checkpoint(progress_dir: Path, fingerprint: str, chunk_count: int) -> None:
    progress_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"version": 1, "fingerprint": fingerprint, "chunk_count": chunk_count}
    manifest_path = progress_dir / "manifest.json"
    try:
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = None
    # Same field-by-field comparison as load_checkpoint: unknown extra fields
    # must not trigger a wipe of previously saved chunks.
    matches = isinstance(existing, dict) and all(
        existing.get(key) == value for key, value in manifest.items()
    )
    if not matches:
        for path in progress_dir.glob("chunk_*.txt"):
            path.unlink(missing_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

def save_checkpoint_chunk(progress_dir: Path, index: int, text: str) -> None:
    destination = progress_dir / f"chunk_{index:04d}.txt"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(destination)
