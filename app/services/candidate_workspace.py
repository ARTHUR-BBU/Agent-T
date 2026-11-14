"""Temporary candidate test inputs. This is not a sandbox or release evidence."""
from __future__ import annotations

import hashlib
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from app.services.rule_release import _full_manifest_at, _full_manifest_paths
from app.services.rule_snapshot import RuleSnapshot

_ROOT = Path(__file__).resolve().parents[2]
_MAX_BYTES = 512 * 1024 * 1024
_MAX_FILES = 20_000


def _bounded_digest(path: Path) -> str:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > _MAX_BYTES:
                raise RuntimeError("候选规则大小超限")
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class CandidateWorkspace:
    root: Path
    source_root: Path
    source_manifest: str
    workspace_manifest: str
    candidate_sha256: str
    category: str

    def verify_integrity(self) -> None:
        """Reject ordinary source or staged input drift before results are used."""
        if (_full_manifest_at(self.source_root, max_files=_MAX_FILES, max_bytes=_MAX_BYTES) != self.source_manifest
                or _full_manifest_at(self.root, max_files=_MAX_FILES, max_bytes=_MAX_BYTES) != self.workspace_manifest):
            raise RuntimeError("回归输入在执行期间发生变化")
        target = self.root / "config" / f"checklist_{self.category}.yaml"
        if _bounded_digest(target) != self.candidate_sha256:
            raise RuntimeError("候选规则在执行期间发生变化")


@contextmanager
def prepare_candidate_workspace(candidate: RuleSnapshot) -> Iterator[CandidateWorkspace]:
    """Copy a checked local input set and substitute candidate bytes only in the copy."""
    if not isinstance(candidate, RuleSnapshot):
        raise TypeError("候选规则无效")
    source = _ROOT.resolve()
    for name in ("app", "config", "tests"):
        if not (source / name).is_dir():
            raise RuntimeError("回归输入不完整")
    target_name = f"checklist_{candidate.category}.yaml"
    original = source / "config" / target_name
    if not original.is_file():
        raise RuntimeError("候选对应的基线规则不存在")
    before = _full_manifest_at(source, max_files=_MAX_FILES, max_bytes=_MAX_BYTES)
    files = _full_manifest_paths(source, max_files=_MAX_FILES)
    with tempfile.TemporaryDirectory(prefix="agent-t-candidate-") as directory:
        dest = Path(directory) / "workspace"
        dest.mkdir()
        total = 0
        for path in files:
            output = dest / path.relative_to(source)
            output.parent.mkdir(parents=True, exist_ok=True)
            with path.open("rb") as src, output.open("xb") as dst:
                while chunk := src.read(1024 * 1024):
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        raise RuntimeError("回归输入大小超限")
                    dst.write(chunk)
        if (_full_manifest_at(source, max_files=_MAX_FILES, max_bytes=_MAX_BYTES) != before
                or _full_manifest_at(dest, max_files=_MAX_FILES, max_bytes=_MAX_BYTES) != before):
            raise RuntimeError("回归输入在复制期间发生变化")
        target = dest / "config" / target_name
        if not target.is_file():
            raise RuntimeError("候选基线复制失败")
        if total - target.stat().st_size + len(candidate.config_bytes) > _MAX_BYTES:
            raise RuntimeError("候选工作区大小超限")
        target.write_bytes(candidate.config_bytes)
        digest = _bounded_digest(target)
        if digest != candidate.content_sha256:
            raise RuntimeError("候选规则复制校验失败")
        workspace = CandidateWorkspace(dest, source, before,
                                       _full_manifest_at(dest, max_files=_MAX_FILES, max_bytes=_MAX_BYTES),
                                       digest, candidate.category)
        yield workspace
        workspace.verify_integrity()
