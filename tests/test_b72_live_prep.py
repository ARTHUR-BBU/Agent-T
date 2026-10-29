# -*- coding: utf-8 -*-
"""B7-2 实跑备料批测试（runner v4 impact 考道 + 考卷前提）。

全程 mock/本地：不 ssh、不 HTTP、不付费。dry-run 前提核对=本地真实
run_checklist+真实检索（免费）；阅卷逻辑用假响应打。
"""
from __future__ import annotations

import json
import pathlib
from unittest import mock

from tools.m7 import adversarial_runner as runner

REPO = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures" / "m7"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------- 考卷清单纪律 ----------

def test_manifest_b72_sanity():
    m = _load("manifest-b72.json")
    assert m["freeze_note"], "B7-2 批也必须带冻结声明"
    assert m["hard_cap_cny"] > 0
    ids = [c["id"] for c in m["cases"]]
    assert len(ids) == len(set(ids)), "例号不得重复"
    for c in m["cases"]:
        assert c["kind"] in ("live", "refuse", "impact_live", "impact_refuse")
        assert c["why"], "每例必须写明考什么"
    kinds = {c["id"]: c["kind"] for c in m["cases"]}
    assert kinds["V1"] == "live" and kinds["V2"] == "live"
    assert kinds["I1"] == "impact_live" and kinds["I2"] == "impact_live"
    assert kinds["R4"] == "impact_refuse"


def test_b71_manifest_frozen_untouched():
    """B7-1 冻结清单不被本批改动（A/C/B 例已实跑，不重复烧钱）。"""
    m = _load("manifest.json")
    assert m["purpose"].startswith("B7-1")
    assert [c["id"] for c in m["cases"]] == \
        ["A1", "A2", "A3", "C1", "C2", "B1", "B2", "B3", "V1", "V2"]


# ---------- dry-run 前提核对（本地真实规则+真实检索，零网络） ----------

def test_dry_run_b72_passes():
    rc, info = runner.premise_check(_load("manifest-b72.json"), FIXTURES)
    assert rc == 0
    assert info["peak_estimate_cny"] <= _load("manifest-b72.json")[
        "hard_cap_cny"], "峰值估算必须压在硬上限内"
    # 4 个 live 例（V1/V2/I1/I2）；I1 贡献 1 次 impact 输出额度
    assert info["n_live"] == 4
    assert info["out_cap_tokens"] == 4 * 16000 + 8000


def test_dry_run_b71_still_passes():
    """runner v4 改造不得破坏 B7-1 冻结清单的前提核对（回归）。"""
    rc, info = runner.premise_check(_load("manifest.json"), FIXTURES)
    assert rc == 0
    assert info["n_live"] == 7


def test_impact_fixture_premises():
    """I1 覆盖前提（排除原条款后摘录>0）/ I2 未覆盖前提（=0）真实成立。"""
    from app.services.checklist import run_checklist
    from app.services.clause_index import build_clause_index, \
        map_items_to_clauses
    from app.services.impact import _exclude_ranges_for_item, \
        find_related_clauses
    counts = {}
    for name in ("i1-impact-linked.txt", "i2-impact-lonely.txt"):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        r = run_checklist(text, "procurement")
        it = next(i for i in r["items"] if i["id"] == "unfair_terms")
        assert it["status"] == "需关注" and (it.get("quote") or "").strip()
        idx = build_clause_index(text)
        map_items_to_clauses(r["items"], idx, text)
        ranges, ids = _exclude_ranges_for_item(it, idx)
        exc = find_related_clauses(text, "unfair_terms",
                                   exclude_positions=ranges,
                                   clause_index=idx, exclude_clause_ids=ids)
        counts[name] = len(exc)
    assert counts["i1-impact-linked.txt"] > 0, "I1 必须命中其他条款（覆盖前提）"
    assert counts["i2-impact-lonely.txt"] == 0, "I2 必须排除后归零（未覆盖前提）"


# ---------- 阅卷逻辑（假响应打，零网络） ----------

def _patch_chain(monkeypatch, revision_body, select_http, impact_body,
                 rev_rows=(), imp_rows=(), budget=(1, 1)):
    """mock HTTP 链与账本/额度查询。"""
    responses = {
        "revision": _resp(200, revision_body),
        "select": _resp(select_http, {"status": "selected"}),
        "impact": _resp(200, impact_body),
    }

    def fake_post(base, auth, url_path, payload, timeout=300):
        if url_path.endswith("/revision"):
            return responses["revision"]
        if url_path.endswith("/select"):
            return responses["select"]
        return responses["impact"]

    monkeypatch.setattr(runner, "_post_json", fake_post)
    monkeypatch.setattr(runner, "_revision_ledger_rows",
                        lambda host, rid, node="revision":
                        list(rev_rows if node == "revision" else imp_rows))
    monkeypatch.setattr(runner, "_budget_used",
                        lambda host, rid: budget)


def _resp(code: int, body: dict):
    r = mock.Mock()
    r.status_code = code
    r.json = lambda: body
    r.text = json.dumps(body, ensure_ascii=False)
    return r


_CASE_I1 = {"id": "I1", "kind": "impact_live", "target_item": "unfair_terms",
            "select_stance": "平衡", "allowed_status": ["ok"],
            "expect": {"covered": True, "impact_llm_calls": 1,
                       "impact_budget_used": 1, "revision_llm_calls": 1}}


def _good_draft(stance_text="折中方案文本。"):
    return {"status": "ok", "draft_id": "d123",
            "draft": {"versions": [
                {"stance": "稳妥", "text": "a", "note": "n"},
                {"stance": "平衡", "text": stance_text, "note": "n"},
                {"stance": "进取", "text": "c", "note": "n"}],
                "rationale": "r", "assumptions": ["假设甲"],
                "original_quote": "乙方违约的定金不予退还。"}}


def test_impact_case_covered_pass(monkeypatch):
    report = {"covered": True, "notice": runner.impact_prompts.RE_REVIEW_NOTICE,
              "selected_stance": "平衡",
              "excerpts": [{"index": 0, "keyword": "违约金", "position": 60,
                            "excerpt": "第十二条 违约责任：…违约金…"}],
              "findings": [{"index": 0, "note": "赔偿口径不一致"}]}
    _patch_chain(monkeypatch, _good_draft(), 200,
                 {"status": "ok", "report": report},
                 rev_rows=[{"call_id": "r1"}], imp_rows=[{"call_id": "i1"}],
                 budget=(1, 1))
    fields, step = runner._run_impact_case(
        base="http://x", auth=("u", "p"), server_host="h", rid="r",
        case=_CASE_I1)
    assert step == "ok" and fields["pass"] is True


def test_impact_case_contaminated_excerpt_fails(monkeypatch):
    """摘录混入被改条款原文（原引句前 12 字出现）→ 阅卷必须不通过。"""
    report = {"covered": True, "notice": "n",
              "selected_stance": "平衡",
              "excerpts": [{"index": 0, "keyword": "违约金", "position": 60,
                            "excerpt": "乙方违约的定金不予退还。 第十二条 …"}],
              "findings": [{"index": 0, "note": "x"}]}
    _patch_chain(monkeypatch, _good_draft(), 200,
                 {"status": "ok", "report": report},
                 rev_rows=[{"call_id": "r1"}], imp_rows=[{"call_id": "i1"}],
                 budget=(1, 1))
    fields, step = runner._run_impact_case(
        base="http://x", auth=("u", "p"), server_host="h", rid="r",
        case=_CASE_I1)
    assert fields["pass"] is False, "污染摘录不得判过"


def test_impact_case_zero_calls_fails_covered_gold(monkeypatch):
    """金标要求 1 次 impact 调用，账本为 0 → 判不通过（防假绿）。"""
    report = {"covered": True, "notice": "n", "selected_stance": "平衡",
              "excerpts": [{"index": 0, "keyword": "违约金", "position": 60,
                            "excerpt": "第十二条 …"}],
              "findings": [{"index": 0, "note": "x"}]}
    _patch_chain(monkeypatch, _good_draft(), 200,
                 {"status": "ok", "report": report},
                 rev_rows=[{"call_id": "r1"}], imp_rows=[], budget=(1, 0))
    fields, step = runner._run_impact_case(
        base="http://x", auth=("u", "p"), server_host="h", rid="r",
        case=_CASE_I1)
    assert fields["pass"] is False


_CASE_I2 = {"id": "I2", "kind": "impact_live", "target_item": "unfair_terms",
            "select_stance": "平衡", "allowed_status": ["uncovered"],
            "expect": {"covered": False, "impact_llm_calls": 0,
                       "impact_budget_used": 0, "revision_llm_calls": 1}}


def test_impact_case_uncovered_pass(monkeypatch):
    report = {"covered": False,
              "notice": runner.impact_prompts.RE_REVIEW_NOTICE,
              "uncovered_reason": "未在合同全文检索到…（不代表没有冲突）"}
    _patch_chain(monkeypatch, _good_draft(), 200,
                 {"status": "uncovered", "report": report},
                 rev_rows=[{"call_id": "r1"}], imp_rows=[], budget=(1, 0))
    fields, step = runner._run_impact_case(
        base="http://x", auth=("u", "p"), server_host="h", rid="r",
        case=_CASE_I2)
    assert step == "ok" and fields["pass"] is True


def test_impact_case_uncovered_wrong_wording_fails(monkeypatch):
    """uncovered 但措辞装作「没问题」（缺「不代表没有冲突」）→ 不通过。"""
    report = {"covered": False,
              "notice": runner.impact_prompts.RE_REVIEW_NOTICE,
              "uncovered_reason": "没有发现任何冲突"}
    _patch_chain(monkeypatch, _good_draft(), 200,
                 {"status": "uncovered", "report": report},
                 rev_rows=[{"call_id": "r1"}], imp_rows=[], budget=(1, 0))
    fields, step = runner._run_impact_case(
        base="http://x", auth=("u", "p"), server_host="h", rid="r",
        case=_CASE_I2)
    assert fields["pass"] is False


def test_impact_case_infra_degrade_classified(monkeypatch):
    _patch_chain(monkeypatch, _good_draft(), 200,
                 {"status": "degraded", "reason": "llm_error"})
    fields, step = runner._run_impact_case(
        base="http://x", auth=("u", "p"), server_host="h", rid="r",
        case=_CASE_I1)
    assert step == "infra", "llm_error 必须归基础设施失败（连续两次即停）"


def test_impact_case_select_rejected_fails(monkeypatch):
    _patch_chain(monkeypatch, _good_draft(), 422, {"status": "ok"})
    fields, step = runner._run_impact_case(
        base="http://x", auth=("u", "p"), server_host="h", rid="r",
        case=_CASE_I1)
    assert step == "fail" and fields["pass"] is None if False else \
        (step == "fail" and not fields.get("pass", False))


def test_draft_over_bound_fails_cost_guard(monkeypatch):
    """选定版草案超长度上界 → 拒判（费用估算上界 integrity）。"""
    _patch_chain(monkeypatch, _good_draft(stance_text="长" * 3000), 200, {})
    fields, step = runner._run_impact_case(
        base="http://x", auth=("u", "p"), server_host="h", rid="r",
        case=_CASE_I1)
    assert step == "fail"
    assert "超上界" in str(fields.get("error"))
