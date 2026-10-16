# -*- coding: utf-8 -*-
"""fixtures-tisvc 语料字节完整性对账（复核门禁要求）。

catdoc 提取文本必须与 MANIFEST.json 登记的 SHA256 逐字节一致——
任何「规范化」（清行尾空格/去末尾空行/改编码）都会在此炸红。
该目录是生产解析忠实重放证据，禁止无声改动；换语料须重跑 catdoc
重放并同步更新 MANIFEST+金标矩阵。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "fixtures-tisvc"


def test_fixture_sha256_matches_manifest() -> None:
    manifest = json.loads((FIXTURE_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest, "MANIFEST.json 为空"
    for fname, entry in sorted(manifest.items()):
        data = (FIXTURE_DIR / fname).read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        assert actual == entry["catdoc_sha256"], (
            f"{fname} 字节漂移：catdoc_sha256 不匹配（现 {actual[:12]}…）"
            "——fixtures 是生产重放证据，禁止规范化改动；如需换语料，"
            "重跑 catdoc 重放并同步更新 MANIFEST+金标矩阵")


def test_manifest_covers_all_fixtures() -> None:
    """MANIFEST 与目录实际文件集合双向对账（防漏登记）。"""
    manifest = json.loads((FIXTURE_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    on_disk = {p.name for p in FIXTURE_DIR.glob("*.txt")}
    assert set(manifest) == on_disk, (
        f"登记/实际不一致：多登={set(manifest) - on_disk} 漏登={on_disk - set(manifest)}")
