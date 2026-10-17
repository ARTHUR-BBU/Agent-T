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


def test_script_gold_matches_manifest() -> None:
    """小智娘 P3-5：校准脚本 GOLD 与 MANIFEST 标签/划分对账（防手工同步漂移）。"""
    import sys

    sys.path.insert(0, str(ROOT / "tools" / "b23"))
    from shadow_calibration import (REJECTION_HOLDOUT, REJECTION_LABELS,
                                    SUPPORT, SUPPORT_HOLDOUT)

    manifest = json.loads((FIXTURE_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    sc = manifest["_meta"]["support_corpus"]
    manifest_gold: dict[str, str] = {}
    for cat, tiers in sc.items():
        if not isinstance(tiers, dict):
            continue
        # MANIFEST 段名 other_reject=八类 other（拒收补位段，老钱五格翻格落点）
        gold_cat = "other" if cat == "other_reject" else cat
        for members in tiers.values():
            if isinstance(members, list):
                for fname in members:
                    manifest_gold[Path(fname).stem] = gold_cat
    for name, _path, label in SUPPORT:
        assert manifest_gold.get(name) == label, (
            f"脚本 GOLD 漂移：{name} 脚本={label} MANIFEST={manifest_gold.get(name)}")
    splits = manifest["_meta"]["splits"]
    manifest_rej_holdout = {Path(x).stem
                            for v in splits.values() for x in v["holdout"]}
    assert set(REJECTION_HOLDOUT) == manifest_rej_holdout, "拒收留出集漂移"
    ss = manifest["_meta"]["support_splits"]["holdout"]
    manifest_sup_holdout = {Path(x).stem for v in ss.values() for x in v}
    assert set(SUPPORT_HOLDOUT) == manifest_sup_holdout, "支持类留出集漂移"
    # 拒收类标签对账（MANIFEST 键带 .doc 后缀；label 用登记期命名，显式映射到八类）
    label_alias = {"consulting_reject": "consulting", "dispatch_reject": "labor_dispatch",
                   "tech_transfer_reject": "tech_transfer"}
    for name, label in REJECTION_LABELS.items():
        ml = manifest[name + ".doc"]["label"]
        ml_canon = label_alias.get(ml, ml)
        assert ml_canon == label, f"拒收标签漂移：{name} MANIFEST={ml} 八类={label}"
