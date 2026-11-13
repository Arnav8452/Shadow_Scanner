"""Nightly copy of the data that cannot be bought back (owner delegated, 13 Nov 2026).

- **What:** ``data/parquet`` (recorded quotes, 30-level books, EOD tables, fundamentals) and
  ``data/state`` (paper journals, setups, study, swing state). Downloadable files
  (``data/raw``) and replays are skipped.
- **How:** copy files that are new or changed (size or modification time) into the target,
  keeping the folder layout. Nothing is ever deleted from the target, so an accidental delete
  or a corrupted day on the source cannot propagate.
- **Where:** ``backup.target`` in settings (default ``C:/QuantBackup``). On a single-disk PC
  that guards against deletion and corruption, not disk failure: point it at an external drive
  for that. No cloud upload by default: private trade logs and personal data stay local.
- **Space:** reports free space on the source and target drives and warns under
  ``backup.min_free_gb``.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BackupResult:
    copied: int = 0
    copied_bytes: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def line(self) -> str:
        mb = self.copied_bytes / 1e6
        out = f"backup: {self.copied} file(s) copied ({mb:,.1f} MB), {self.skipped} unchanged"
        if self.errors:
            out += f", {len(self.errors)} error(s)"
        return out + "".join(f"; WARNING {w}" for w in self.warnings)


def _free_gb(path: Path) -> float | None:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / 1e9
    except OSError:
        return None


def run_backup(
    root: Path, sources: Sequence[str], target: Path, *, min_free_gb: float = 5.0
) -> BackupResult:
    res = BackupResult()
    target.mkdir(parents=True, exist_ok=True)
    for rel in sources:
        src = (root / rel).resolve()
        if not src.exists():
            continue
        for f in src.rglob("*"):
            if not f.is_file():
                continue
            dst = target / f.relative_to(root.resolve())
            try:
                st = f.stat()
                if dst.exists():
                    dt = dst.stat()
                    if dt.st_size == st.st_size and dt.st_mtime >= st.st_mtime - 1:
                        res.skipped += 1
                        continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
                res.copied += 1
                res.copied_bytes += st.st_size
            except OSError as exc:  # a file being written right now: next night
                res.errors.append(f"{f.name}: {type(exc).__name__}")
    for label, path in (("source", root), ("backup target", target)):
        free = _free_gb(path)
        if free is not None and free < min_free_gb:
            res.warnings.append(f"{label} drive has {free:.1f} GB free (< {min_free_gb:g} GB)")
    return res


__all__ = ["BackupResult", "run_backup"]
