"""外部审计批 2 整改测试：unless 逐 occurrence、TTL 读时过期、配置启动校验、
解析异常收口、HF 依赖锁。

对应「代码质量审查报告｜第一轮」§五/§六/§七/§九/§十。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.services import checklist as checklist_service
from app.services.checklist import _eval_item, validate_checklist_configs
from app.services.store import ReviewStore

ROOT = Path(__file__).resolve().parents[1]


# ---------- 批2-① unless 逐 occurrence 判定（双向金标） ----------

_RULE_SUBLET = {
    "id": "test_sublet",
    "name": "转租",
    "segment": "A",
    "rules": {
        "need_attention": [
            {
                "any_of": ["转租"],
                "note": "疑似转租",
                "unless": {"any_of": ["产权人书面同意", "经产权人同意"]},
            }
        ],
        "pass": [{"any_of": ["自有权房屋出租"]}],
    },
}

_FAR = "双方另行约定了与租赁标的无关的装修维护事项。" * 8  # 拉开两 occurrence 距离（超出 ±60 窗口，且不含「转租」字样）


def test_front_safe_back_dangerous_fires():
    """双向金标·正向：前文保护句不得洗掉后文独立风险句（旧实现漏检场景）。"""
    text = "经产权人书面同意，甲方可将房屋转租。" + _FAR + "本房屋系出租方转租取得。"
    out = _eval_item(text, _RULE_SUBLET)
    assert out["status"] == "需关注", "后文无保护的转租 occurrence 必须触发风险"


def test_front_dangerous_back_safe_fires():
    """双向金标·反向：前文风险 + 后文保护句（各自独立时风险仍成立）。"""
    text = "本房屋系出租方转租取得。" + _FAR + "经产权人书面同意，甲方可将房屋转租。"
    out = _eval_item(text, _RULE_SUBLET)
    assert out["status"] == "需关注"


def test_all_occurrences_protected_passes():
    """全部 occurrence 邻域都有保护性表述 → 放行（unless 语义不回退）。"""
    text = "经产权人书面同意，甲方可将房屋转租。" + _FAR + "再次转租亦须产权人书面同意后方可进行。"
    out = _eval_item(text, _RULE_SUBLET)
    assert out["status"] != "需关注", "全部受保护的命中应被 unless 放行"


def test_single_protected_occurrence_still_passes():
    """单个受保护 occurrence：与历史行为一致（放行）——不因新逻辑误伤。"""
    text = "经产权人书面同意，甲方可将房屋转租。"
    out = _eval_item(text, _RULE_SUBLET)
    assert out["status"] != "需关注"


def test_rule_without_unless_unchanged():
    """无 unless/none_of 的规则完全不受本改动影响。"""
    rule = {
        "id": "t",
        "name": "t",
        "segment": "A",
        "rules": {
            "need_attention": [{"any_of": ["没收押金"], "note": "n"}],
            "pass": [{"any_of": ["正常"]}],
        },
    }
    assert _eval_item("合同约定没收押金。", rule)["status"] == "需关注"
    assert _eval_item("正常条款。", rule)["status"] != "需关注"


# ---------- 批2-② TTL 读时强制过期 ----------

def test_expired_record_inaccessible_via_get_and_update(tmp_path):
    # 窗口 0.5s（负载下 create→get 超 0.05s 即脆断，同 test_audit_fixes 加固）
    store = ReviewStore(db_path=str(tmp_path / "t.db"), ttl_seconds=0.5)
    rid = store.create(filename="a.txt", category="lease", status="done")
    assert store.get(rid) is not None, "TTL 内可读"
    assert store.update(rid, filename="b.txt") is not None
    time.sleep(0.6)
    assert store.get(rid) is None, "过期后 get 必须视为不存在（不再依赖 create 触发 purge）"
    assert store.update(rid, filename="c.txt") is None, "过期后 update 不得复活记录"


def test_ttl_zero_means_never_expire(tmp_path):
    store = ReviewStore(db_path=str(tmp_path / "t.db"), ttl_seconds=0)
    rid = store.create(filename="a.txt", category="lease")
    time.sleep(0.02)
    assert store.get(rid) is not None, "TTL=0 = 永不过期（既有语义不变）"


# ---------- 批2-④ 配置启动校验 fail-closed ----------

def test_current_configs_pass_validation():
    """现有三份清单全部通过校验（CI 与启动同款检查）。"""
    validate_checklist_configs()  # 不抛即过


def test_bad_regex_config_fails_closed(tmp_path, monkeypatch):
    bad_yaml = tmp_path / "checklist_bad.yaml"
    bad_yaml.write_text(
        """
category: bad
label: 坏配置
items:
  - id: x
    name: x
    segment: A
    rules:
      need_attention:
        - any_of: ["(未闭合括号"]
          note: n
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(checklist_service, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError, match="非法正则"):
        validate_checklist_configs()


def test_duplicate_item_id_fails_closed(tmp_path, monkeypatch):
    dup_yaml = tmp_path / "checklist_dup.yaml"
    dup_yaml.write_text(
        """
category: dup
label: 重复id
items:
  - id: same
    name: a
    segment: A
    rules: {need_attention: [{any_of: ["甲"], note: n}], pass: [{any_of: ["乙"]}]}
  - id: same
    name: b
    segment: A
    rules: {need_attention: [{any_of: ["丙"], note: n}], pass: [{any_of: ["丁"]}]}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(checklist_service, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError, match="item id 重复"):
        validate_checklist_configs()


# ---------- 批2-⑤ node_parse 异常收口 ----------

def test_node_parse_unknown_exception_fixed_message(monkeypatch):
    from app.graph import pipeline

    def boom(*a, **kw):
        raise RuntimeError("内部路径 /usr/lib/python3/site-packages/docling/x.py 泄露")

    monkeypatch.setattr(pipeline, "extract_text", boom)
    out = pipeline.node_parse({"filename": "a.pdf", "raw_bytes": b"x"})
    assert out["error"] == "文档解析失败，请重新上传或转换格式后再试"
    assert "docling" not in out["error"] and "/usr/lib" not in out["error"]


def test_node_parse_extraction_error_keeps_controlled_message(monkeypatch):
    from app.graph import pipeline
    from app.services.extract import ExtractionError

    def raise_controlled(*a, **kw):
        raise ExtractionError("文件已损坏，无法解析")

    monkeypatch.setattr(pipeline, "extract_text", raise_controlled)
    out = pipeline.node_parse({"filename": "a.docx", "raw_bytes": b"x"})
    assert out["error"] == "文件已损坏，无法解析", "设计为用户可见的受控文案原样保留"


# ---------- 批2-⑤ HF Dockerfile 依赖锁统一 ----------

def test_hf_dockerfile_uses_lock():
    content = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements-lock.txt" in content, "HF 演示镜像必须与 CI/服务器同源走 lock"
    assert "-r requirements.txt" not in content, "不得再引用无锁的 requirements.txt"
