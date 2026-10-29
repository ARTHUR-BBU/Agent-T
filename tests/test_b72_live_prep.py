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
                 rev_rows=(), imp_rows=(), budget=(1, 1),
                 strict_spent=(0, 0, 0)):
    """mock HTTP 链与账本/额度/严格圈账查询（零网络零 ssh）。"""
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
    monkeypatch.setattr(runner, "_spent_strict",
                        lambda host, rids: strict_spent)
    monkeypatch.setattr(runner, "_node_ledger_status",
                        lambda host, rid, node: (1, 0))


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


# ---------- #156 复批 1：默认考卷=B7-2 新卷，开跑亮卷 ----------

def test_default_manifest_is_b72():
    assert runner.DEFAULT_MANIFEST == "manifest-b72.json", \
        "默认卷必须是 B7-2 新卷（防默认拿错卷重复付费考旧题）"
    assert (FIXTURES / runner.DEFAULT_MANIFEST).exists()


def test_run_live_prints_manifest_identity(tmp_path, monkeypatch, capsys):
    """--live 开跑先亮卷（卷名+用途+例号），复核人可一眼核对考的是什么。"""
    monkeypatch.setattr(runner, "run_dry", lambda m, f: 1)  # 预检失败即停
    m = _load("manifest-b72.json")
    rc = runner.run_live(m, "http://x", "auth", "host",
                         tmp_path / "out.json", manifest_name="manifest-b72.json")
    out = capsys.readouterr().out
    assert rc == 1
    assert "manifest-b72.json" in out
    assert "V1" in out and "R4" in out, "例号必须在亮卷行中"


# ---------- #156 复批 2：受限态≠未覆盖金标（I2 假绿封堵） ----------

def test_i2_restricted_wording_fails(monkeypatch):
    """缺索引受限态（无法可靠定位）冒充未覆盖 → 阅卷必须不通过。"""
    report = {"covered": False,
              "notice": runner.impact_prompts.RE_REVIEW_NOTICE,
              "uncovered_reason": "无法可靠定位被修改条款的完整范围（缺条款"
                                  "索引或无法定位条款边界），为避免把原条款"
                                  "误当关联条款，本次不进行关联分析（不代表"
                                  "没有冲突）"}
    _patch_chain(monkeypatch, _good_draft(), 200,
                 {"status": "uncovered", "report": report},
                 rev_rows=[{"call_id": "r1"}], imp_rows=[], budget=(1, 0))
    fields, _step = runner._run_impact_case(
        base="http://x", auth=("u", "p"), server_host="h", rid="r",
        case=_CASE_I2)
    assert fields["pass"] is False, \
        "受限态不是「找过但没找到」，不得按未覆盖金标判过"


def test_i2_found_nothing_wording_passes(monkeypatch):
    """真·找过但没找到（检索措辞）→ 按未覆盖金标判过。"""
    report = {"covered": False,
              "notice": runner.impact_prompts.RE_REVIEW_NOTICE,
              "uncovered_reason": "未在合同全文检索到该条款之外可能与草案联动"
                                  "的其他条款（不代表没有冲突）"}
    _patch_chain(monkeypatch, _good_draft(), 200,
                 {"status": "uncovered", "report": report},
                 rev_rows=[{"call_id": "r1"}], imp_rows=[], budget=(1, 0))
    fields, _step = runner._run_impact_case(
        base="http://x", auth=("u", "p"), server_host="h", rid="r",
        case=_CASE_I2)
    assert fields["pass"] is True


# ---------- #156 复批 3/4：验收表独立文件+不覆盖历史+补关键正文 ----------

def _impact_rec() -> dict:
    d = _good_draft()["draft"]
    d["original_quote"] = "乙方违约的定金不予退还。"
    return {"id": "I1", "kind": "impact_live", "pass": True,
            "impact_status": "ok", "select_stance": "平衡",
            "impact_llm_calls": 1, "impact_budget_used": 1,
            "draft": d,
            "impact_report": {"covered": True,
                              "notice": runner.impact_prompts.RE_REVIEW_NOTICE,
                              "selected_stance": "平衡",
                              "excerpts": [{"index": 0, "keyword": "违约金",
                                            "position": 60, "excerpt": "第十"
                                            "二条 违约责任：…"}],
                              "findings": [{"index": 0, "note": "赔偿口径不"
                                            "一致"}]}}


def test_semantic_rows_b72_separate_file(tmp_path):
    """B7-2 批写独立文件，绝不写 B7-1 历史验收表文件名。"""
    runner._emit_semantic_rows([_impact_rec()], tmp_path,
                               manifest_name="manifest-b72.json")
    assert (tmp_path / "semantic-acceptance-b7-2-filled.md").exists()
    assert not (tmp_path / "semantic-acceptance-b7-1-filled.md").exists()


def test_semantic_rows_refuse_overwrite_history(tmp_path):
    """目标文件已存在=拒绝覆盖（保护历史验收记录，codex #156 复批 3）。"""
    target = tmp_path / "semantic-acceptance-b7-2-filled.md"
    target.write_text("历史记录", encoding="utf-8")
    try:
        runner._emit_semantic_rows([_impact_rec()], tmp_path,
                                   manifest_name="manifest-b72.json")
        raised = False
    except SystemExit:
        raised = True
    assert raised, "已存在的验收记录必须拒绝覆盖"
    assert target.read_text(encoding="utf-8") == "历史记录", "旧记录原样保留"


def test_semantic_rows_impact_prefill_carries_quotes(tmp_path):
    """预填必须带被改原文+选中草案正文（老钱逐项对照的依据）。"""
    runner._emit_semantic_rows([_impact_rec()], tmp_path,
                               manifest_name="manifest-b72.md".replace(".md", ".json"))
    text = (tmp_path / "semantic-acceptance-b7-2-filled.md").read_text(
        encoding="utf-8")
    assert "乙方违约的定金不予退还。" in text, "被改条款原文必须在场"
    assert "折中方案文本。" in text, "选中版草案正文必须在场"
    assert "选定版别：平衡" in text
    assert "赔偿口径不一致" in text, "findings 逐条在场"


# ---------- #156 复批 5：可执行预算边界 ----------

def test_can_afford_next_boundary():
    """发请求前校验：累计+本请求最坏 < 上限；累计未知=绝不放行。"""
    assert runner._can_afford_next(0.5, 0.13, 2.0) is True
    assert runner._can_afford_next(1.88, 0.132, 2.0) is False, \
        "累计+最坏 ≥ 上限必须拦截（不能靠批后对账）"
    assert runner._can_afford_next(None, 0.13, 2.0) is False, \
        "费用未知绝不放行下一请求"


def test_case_io_bound_matches_premise_scale():
    """单例上界测量与 premise 汇总同一口径（I1=2 次调用上界）。"""
    m = _load("manifest-b72.json")
    case = next(c for c in m["cases"] if c["id"] == "I1")
    text = (FIXTURES / case["file"]).read_text(encoding="utf-8")
    from app.services.checklist import run_checklist
    cl = run_checklist(text, case["category"])
    in_ch, out_t = runner._case_io_bound(case, text, cl)
    assert out_t == 16000 + 8000, "I1 最坏=1 次 revision+1 次 impact 输出"
    assert in_ch > 0
    assert runner._worst_cny(in_ch, out_t) < 2.0, "单例最坏必须小于硬上限"


# ---------- #156 三轮 P1：例内第二笔付费前查账（I1 控制流） ----------

_I1_MANIFEST = {"purpose": "I1 单例编排测试", "hard_cap_cny": 2.0,
                "cases": [dict(_CASE_I1, file="i1-impact-linked.txt",
                               category="procurement")]}


def test_i1_unknown_usage_blocks_impact(tmp_path, monkeypatch, capsys):
    """run_live 编排路径：revision 成功但账本缺用量 → 第二笔 impact 不得
    发出、后续样本不启动、结果失败且费用未知。"""
    paths: list[str] = []

    def fake_post(base, auth, url_path, payload, timeout=300):
        paths.append(url_path)
        if url_path.endswith("/revision"):
            return _resp(200, _good_draft())
        raise AssertionError("查账未知后不得发出 select/impact 请求")

    monkeypatch.setattr(runner, "_post_json", fake_post)
    monkeypatch.setattr(runner, "_seed_review", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "server_revision", lambda host: "x")
    monkeypatch.setattr(runner, "check_revision", lambda c, t: (True, ""))
    monkeypatch.setattr(runner, "deepseek_balance", lambda key: 99.0)
    # 第一笔 revision 后查账即发现缺用量（后续任何查账调用同答）
    monkeypatch.setattr(runner, "_spent_strict",
                        lambda host, rids: (0, 0, 1, {}))
    monkeypatch.setattr(runner, "_node_ledger_status",
                        lambda host, rid, node: (0, 0))
    auth = tmp_path / "auth"
    auth.write_text("user=u\npassword=p\ndeepseek_key=dk\n", encoding="utf-8")
    rc = runner.run_live(_I1_MANIFEST, "http://x", str(auth), "host",
                         tmp_path / "out.json",
                         manifest_name="manifest-b72.json")
    out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert rc == 1
    sent = [p.rsplit("/", 1)[-1] for p in paths]
    assert sent == ["revision"], f"只应发出第一笔 revision，实际 {sent}"
    assert "impact" not in sent and "select" not in sent, "缺用量后不得续发"
    assert out["cost_known"] is False, "费用未知必须在最终记录保留（不冒充已知）"
    assert out["stop_reason"], "必须有停止原因"
    assert "费用未知" in out["stop_reason"]
    rec = out["results"][0]
    assert rec["pass"] is False and rec.get("cost_unknown") is True


def test_i1_normal_usage_two_calls_pass(tmp_path, monkeypatch):
    """正常用量时 I1 两步调用链完整走通（对照：查账不误伤）。"""
    paths: list[str] = []
    report = {"covered": True, "notice": runner.impact_prompts.RE_REVIEW_NOTICE,
              "selected_stance": "平衡",
              "excerpts": [{"index": 0, "keyword": "违约金", "position": 60,
                            "excerpt": "第十二条 违约责任：…"}],
              "findings": [{"index": 0, "note": "赔偿口径不一致"}]}

    def fake_post(base, auth, url_path, payload, timeout=300):
        paths.append(url_path)
        if url_path.endswith("/revision"):
            return _resp(200, _good_draft())
        if url_path.endswith("/select"):
            return _resp(200, {"status": "selected"})
        return _resp(200, {"status": "ok", "report": report})

    monkeypatch.setattr(runner, "_post_json", fake_post)
    monkeypatch.setattr(runner, "_seed_review", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "server_revision", lambda host: "x")
    monkeypatch.setattr(runner, "check_revision", lambda c, t: (True, ""))
    monkeypatch.setattr(runner, "deepseek_balance", lambda key: 99.0)
    monkeypatch.setattr(runner, "_spent_strict",
                        lambda host, rids: (100, 200, 0,
                        {r: [100, 200, 0, 1] for r in rids}))
    monkeypatch.setattr(runner, "_node_ledger_status",
                        lambda host, rid, node: (1, 0))
    monkeypatch.setattr(runner, "_revision_ledger_rows",
                        lambda host, rid, node="revision":
                        [{"call_id": node}])
    monkeypatch.setattr(runner, "_budget_used", lambda host, rid: (1, 1))
    auth = tmp_path / "auth"
    auth.write_text("user=u\npassword=p\ndeepseek_key=dk\n", encoding="utf-8")
    rc = runner.run_live(_I1_MANIFEST, "http://x", str(auth), "host",
                         tmp_path / "out.json",
                         manifest_name="manifest-b72.json")
    out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert rc == 0, out.get("stop_reason")
    sent = [p.rsplit("/", 1)[-1] for p in paths]
    assert sent == ["revision", "select", "impact"], "两步调用链完整"
    assert out["results"][0]["pass"] is True


# ---------- #156 二轮 P1-1：费用未知≠零元（严格圈账） ----------

def _run_sql_fragment(code: str, db: str, args: tuple):
    import subprocess
    r = subprocess.run(["python", "-X", "utf8", "-c", code, db, *args],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().splitlines()[-1]


def test_strict_spent_missing_usage_is_unknown(tmp_path):
    """codex 复现：usage={} 的真实请求行必须计为「费用未知」而非零元。"""
    import sqlite3
    db = str(tmp_path / "led.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE llm_calls (call_id TEXT, node TEXT,"
                " review_id TEXT, outcome TEXT, error_detail TEXT,"
                " raw_output TEXT, usage TEXT)")
    def call(cid, outcome, usage):
        con.execute("INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?)",
                    (cid, "revision", "r1", outcome, None, None, usage))
    call("c1", "success", json.dumps({"input_tokens": 10,
                                      "output_tokens": 20}))
    call("c2", "provider_error", "{}")            # 已发请求缺用量
    call("c3", "timeout", None)                   # 超时无用量
    call("c4", "budget_exceeded", None)           # skip 行：本就无调用
    con.commit()
    con.close()
    out = _run_sql_fragment(runner.STRICT_SPENT_CODE, db, args=("r1",))
    ins, outs, unknown, per = json.loads(out)
    assert (ins, outs) == (10, 20), "有用量行正常累计"
    assert unknown == 2, "缺用量的真实请求行必须计为未知（skip 行不计）"
    assert per["r1"][3] == 3, "per-rid 真实行数=3（skip 行不计）"


def test_node_rows_fragment(tmp_path):
    """节点级核对片段：有行+用量全=（1,0）；零行=（0,0）；skip 行不计。"""
    import sqlite3
    db = str(tmp_path / "led.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE llm_calls (call_id TEXT, node TEXT,"
                " review_id TEXT, outcome TEXT, error_detail TEXT,"
                " raw_output TEXT, usage TEXT)")
    con.execute("INSERT INTO llm_calls VALUES ('c1','revision','r1',"
                " 'success',NULL,NULL,?)",
                (json.dumps({"input_tokens": 5, "output_tokens": 6}),))
    con.execute("INSERT INTO llm_calls VALUES ('c2','revision','r1',"
                " 'budget_exceeded',NULL,NULL,NULL)")
    con.commit()
    con.close()
    out = _run_sql_fragment(runner.NODE_ROWS_CODE, db, args=("r1", "revision"))
    assert json.loads(out) == [1, 0], "真实行 1（skip 不计），用量完整 0 未知"
    out2 = _run_sql_fragment(runner.NODE_ROWS_CODE, db, args=("ghost", "revision"))
    assert json.loads(out2) == [0, 0], "零行=（0,0）——调用方须按费用未知处理"


def test_i1_zero_ledger_rows_blocks_impact(monkeypatch):
    """四轮 P1：revision 成功但账本连行都没有（写入失败）→ 不发 select/
    impact，费用未知（零行≠零元）。"""
    paths: list[str] = []

    def fake_post(base, auth, url_path, payload, timeout=300):
        paths.append(url_path.rsplit("/", 1)[-1])
        if url_path.endswith("/revision"):
            return _resp(200, _good_draft())
        raise AssertionError("缺账行后不得发出 select/impact")

    monkeypatch.setattr(runner, "_post_json", fake_post)
    monkeypatch.setattr(runner, "_node_ledger_status",
                        lambda host, rid, node: (0, 0))  # 零行
    fields, step = runner._run_impact_case(
        base="http://x", auth=("u", "p"), server_host="h", rid="r",
        case=_CASE_I1)
    assert step == "cost_unknown"
    assert paths == ["revision"], "只应发出第一笔，第二笔必须被拦下"
    assert "费用未知" in str(fields.get("error"))


def test_account_expect_paid_zero_rows_is_unknown(monkeypatch):
    """四轮 P1：预计已发付费的单缺账行 → 费用未知返回 None；
    本地拒绝例（不在 expect_paid）零行不受影响。"""
    monkeypatch.setattr(runner, "_spent_strict", lambda host, rids: (
        0, 0, 0, {"r_paid": [0, 0, 0, 0], "r_refuse": [0, 0, 0, 0]}))
    assert runner._account("h", ["r_paid", "r_refuse"],
                           expect_paid=("r_paid",)) is None, \
        "应发账却零行=费用未知"
    assert runner._account("h", ["r_refuse"]) == 0.0, \
        "本地拒绝例本就零行=真零元"


def test_account_returns_none_when_usage_missing(monkeypatch):
    """圈账遇 unknown>0 → 返回 None（调用方停止后续付费请求）。"""
    monkeypatch.setattr(runner, "_spent_strict",
                        lambda host, rids: (10, 20, 1, {}))
    assert runner._account("h", ["r1"]) is None, "费用未知绝不冒充金额"


def test_account_zero_rows_is_real_zero(monkeypatch):
    """无任何真实请求行 → 真·零元（与未知严格区分）。"""
    monkeypatch.setattr(runner, "_spent_strict",
                        lambda host, rids: (0, 0, 0, {}))
    assert runner._account("h", ["r1"]) is not None
    assert runner._account("h", ["r1"]) == 0.0


# ---------- #156 二轮 P1-2：防覆盖检查前移（run_live 级） ----------

def test_run_live_refuses_existing_outputs_before_anything(tmp_path, monkeypatch, capsys):
    """结果 JSON 或验收表已存在 → 开跑即拒：零凭据、零 HTTP、零种单、
    零付费（不是等跑完才在写入时拒绝）。"""
    blocker = tmp_path / "semantic-acceptance-b7-2-filled.md"
    blocker.write_text("历史记录", encoding="utf-8")
    def _explode(*a, **kw):
        raise AssertionError("目标已存在时不得发生任何副作用（HTTP/种单/凭据）")
    monkeypatch.setattr(runner.httpx, "post", _explode)
    monkeypatch.setattr(runner, "_seed_review", _explode)
    monkeypatch.setattr(runner, "_auth", _explode)
    monkeypatch.setattr(runner, "run_dry",
                        lambda m, f: (_explode(), 1)[1])  # 连预检都不该到
    m = _load("manifest-b72.json")
    rc = runner.run_live(m, "http://x", "auth", "host",
                         tmp_path / "out.json",
                         manifest_name="manifest-b72.json")
    out = capsys.readouterr().out
    assert rc == 1 and "拒绝开跑" in out
    assert blocker.read_text(encoding="utf-8") == "历史记录", "旧文件原样保留"


def test_run_live_refuses_existing_result_json(tmp_path, monkeypatch, capsys):
    """--out 的逐例 JSON 已存在 → 同样开跑即拒（flush 会覆盖旧 JSON）。"""
    result = tmp_path / "out.json"
    result.write_text('{"old": true}', encoding="utf-8")
    def _explode(*a, **kw):
        raise AssertionError("目标已存在时不得发生任何副作用")
    monkeypatch.setattr(runner.httpx, "post", _explode)
    monkeypatch.setattr(runner, "run_dry", lambda m, f: 1)
    m = _load("manifest-b72.json")
    rc = runner.run_live(m, "http://x", "auth", "host", result,
                         manifest_name="manifest-b72.json")
    assert rc == 1 and "拒绝开跑" in capsys.readouterr().out
    assert json.loads(result.read_text(encoding="utf-8")) == {"old": True}
