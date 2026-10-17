# -*- coding: utf-8 -*-
"""fixtures-classification 语料字节完整性对账（B2-2 同款守卫，B2-3 批2）。

catdoc 提取文本必须与 MANIFEST.json 登记的 extract_sha256 逐字节一致——
任何「规范化」（清行尾空格/去末尾空行/改编码）或 git 换行转换污染都会在此炸红。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "fixtures-classification"


def test_extract_sha256_matches_manifest() -> None:
    manifest = json.loads((FIXTURE_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    checked = 0
    for fname, entry in manifest.items():
        if fname.startswith("_"):
            continue
        extract = entry["extract_path"]
        data = (ROOT / extract).read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        assert actual == entry["extract_sha256"], (
            f"{extract} 字节漂移——fixtures 是生产重放证据，禁止规范化改动；"
            "换语料须重跑 catdoc 重放并同步更新 MANIFEST")
        checked += 1
    assert checked == 9, f"应核对 9 份，实核 {checked}"


def test_manifest_covers_all_extract_files() -> None:
    manifest = json.loads((FIXTURE_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    on_disk = {p.name for p in FIXTURE_DIR.glob("*.txt")}
    registered = {Path(e["extract_path"]).name
                  for k, e in manifest.items() if not k.startswith("_")}
    assert registered == on_disk, (
        f"登记/实际不一致：多登={registered - on_disk} 漏登={on_disk - registered}")
