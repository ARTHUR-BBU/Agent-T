"""租赁品类金标测试（承租方视角）。

断言来源：docs/lease-category-legal-opinion.md §六（法务老钱 2026-09-06）。
- 规则层：各 fixture 的档位必须命中预期（漏报/误报都算失败）
- 评分层：模型谎报满分时，代码重算 + 封顶必须压住（≤74 / ≤89 / ≤66）
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services import scorecard
from app.services.blind_spot import annotate_rule_items
from app.services.checklist import run_checklist
from app.services.model_review import run_model_review

ROOT = Path(__file__).resolve().parents[1]
LEASE = ROOT / "fixtures"


def _items(name: str) -> dict[str, dict]:
    text = (LEASE / name).read_text(encoding="utf-8")
    result = run_checklist(text, category="lease")
    return {it["id"]: it for it in result["items"]}


def _model_full_marks():
    """模型给满分的 payload（total 自称 100、各段满分），供封顶测试。"""
    segments = scorecard.load_scorecard_config("lease")["segments"]
    return json.dumps(
        {
            "scorecard": {
                "total": 100,
                "summary": "条款完备，可以放心签署。",
                "segments": [
                    {"key": s["key"], "score": s["weight"]} for s in segments
                ],
            },
            "candidates": [],
        },
        ensure_ascii=False,
    )


def _capped_total(name: str) -> int:
    """模型满分 + 代码重算/封顶后的总分。"""
    text = (LEASE / name).read_text(encoding="utf-8")
    items = annotate_rule_items(run_checklist(text, "lease")["items"])
    segments = scorecard.load_scorecard_config("lease")["segments"]
    hard_names = "、".join(
        i["name"] for i in items if i["status"] != "通过" and not i.get("category_na")
    )

    def chat(_system, _user):
        return _model_full_marks()

    # 评语点名全部硬伤（空评语段由代码自动补点名，不触发降级）
    payload = json.loads(_model_full_marks())
    payload["scorecard"]["segments"][0]["comment"] = "点名：" + hard_names
    out = run_model_review(
        text=text, items=items, policies=[], category="lease",
        chat_fn=lambda s, u: json.dumps(payload, ensure_ascii=False),
    )
    sc = out["scorecard"]
    assert sc["available"] is True
    total_sum = sum(s["score"] for s in sc["segments"])
    # 封顶触发时 total = min(分段和, cap)，故只断言 total 不超过分段和（封顶测试断言上限值）
    assert sc["total"] <= total_sum, "总分必须由分段重算（封顶只降不升）"
    return sc["total"]


# ---------- 断言 1：退出权绑死（≤74，D 段核心封顶） ----------

def test_early_termination_binding_gold():
    by_id = _items("lease_early_term.txt")
    assert by_id["early_termination"]["status"] == "需关注", "剩余租期全额违约金必须需关注"
    # 单风险隔离：其余风险项保持通过，证明封顶确实由 early_termination 触发
    for iid in ("deposit", "maintenance", "rent_payment", "governing_law", "signature"):
        assert by_id[iid]["status"] == "通过", f"{iid} 应通过，实际 {by_id[iid]['status']}"
    assert _capped_total("lease_early_term.txt") <= 74


# ---------- 断言 2：押金没收 + 断水断电催租（≤89） ----------

def test_deposit_and_cut_utilities_gold():
    by_id = _items("lease_deposit_late.txt")
    flagged = {
        iid for iid in ("rent_payment", "deposit")
        if by_id[iid]["status"] == "需关注"
    }
    assert flagged, "押金不予退还 + 逾期断水断电至少一项需关注"
    assert _capped_total("lease_deposit_late.txt") <= 89


# ---------- 断言 4：维修义务倒挂（≤89） ----------

def test_maintenance_inverted_gold():
    by_id = _items("lease_maintenance.txt")
    assert by_id["maintenance"]["status"] == "需关注", "一切维修归乙方必须需关注"
    assert _capped_total("lease_maintenance.txt") <= 89


# ---------- 断言 5：同构回归（管辖有/适用法律无 + 仅盖章无签字） ----------

def test_governing_law_and_signature_gold():
    by_id = _items("lease_sample.txt")
    assert by_id["jurisdiction"]["status"] == "通过", "有管辖条款应通过"
    assert by_id["governing_law"]["status"] == "需关注", "仅有管辖无适用法律必须需关注"
    assert by_id["signature"]["status"] == "需关注", "仅盖章无签字必须需关注"


# ---------- 断言 6：四连压力测试（≤66） ----------

def test_lease_four_risk_rules_flag():
    by_id = _items("lease_four_risk.txt")
    for iid in ("early_termination", "deposit", "renovation", "maintenance"):
        assert by_id[iid]["status"] == "需关注", f"{iid} 漏报"
    assert by_id["governing_law"]["status"] == "需关注", "缺适用法律必须需关注"
    assert by_id["signature"]["status"] == "需关注", "仅盖章必须需关注"
    # D 段三项全挂 + E/F/G 失分
    core_flagged = [
        i for i in by_id.values()
        if i.get("segment") in ("B", "D") and i["status"] in ("需关注", "未找到")
    ]
    assert core_flagged, "D 段无靶点，封顶逻辑不会触发"


def test_lease_four_risk_capped_66():
    total = _capped_total("lease_four_risk.txt")
    assert total <= 66, f"四连压力封顶 66 失效，实际 {total}"
    assert total != 100


# ---------- 防误报：良性合同不得误杀 ----------

def test_lease_sample_benign_items_pass():
    by_id = _items("lease_sample.txt")
    for iid in ("subject", "lessor_title", "lease_term", "delivery_acceptance",
                "use_restriction", "rent_payment", "deposit", "renovation",
                "maintenance", "subletting", "early_termination"):
        assert by_id[iid]["status"] == "通过", (
            f"{iid} 在良性合同上被误杀：{by_id[iid]['status']} {by_id[iid]['note']}"
        )


def test_lease_renovation_negation_not_flagged():
    """「退租无需恢复原状」是承租方友好条款，不得命中恢复原状负担模式（开发狗调优）。"""
    by_id = _items("lease_early_term.txt")
    assert by_id["renovation"]["status"] == "通过"


# ---------- 评分卡配置自检 ----------

def test_lease_scorecard_config_valid():
    segments = scorecard.load_scorecard_config("lease")["segments"]
    assert [s["key"] for s in segments] == ["A", "B", "C", "D", "E", "F", "G"]
    assert sum(s["weight"] for s in segments) == 100, "七段权重必须合计 100（无 NA 段直加）"


# ---------- 小智娘终验：新规则的三处误伤修复 ----------

def test_protective_sublease_boilerplate_not_flagged():
    """「未经产权人同意，乙方不得转租」是转租须经同意的保护性条款（民法典716
    默认安排），不是权属缺陷信号，不得命中 lessor_title 需关注."""
    text = (
        "出租方（甲方）：某某置业有限公司，法定代表人：张三，住所：某某市某某区某某路1号。\n"
        "承租方（乙方）：某某科技有限公司。甲方系房屋产权人，持不动产权证，依法出租。\n"
        "租赁期限自2026年10月1日起至2027年9月30日止。月租金1万元，押二付三。\n"
        "未经产权人同意，乙方不得转租。\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["lessor_title"]["status"] == "通过", (
        f"保护性 boilerplate 被误伤：{by_id['lessor_title']['status']} {by_id['lessor_title']['note']}"
    )


def test_prohibited_utility_cutoff_not_flagged():
    """「甲方不得对房屋断水断电」是承租方友好条款，不得命中 rent_payment 需关注."""
    text = (
        "出租方（甲方）：某某置业有限公司，法定代表人：张三，住所：某某市某某区某某路1号。\n"
        "承租方（乙方）：某某科技有限公司。甲方系房屋产权人，持不动产权证，依法出租。\n"
        "租赁期限自2026年10月1日起至2027年9月30日止。月租金1万元，押二付三。\n"
        "租赁期满乙方可续租。装修归乙方所有。维修由甲方负责。违约金按未履行部分租金的百分之二十计算。\n"
        "甲方不得对房屋断水断电，不得以任何方式影响乙方正常经营。\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["rent_payment"]["status"] == "通过", (
        f"禁止断水断电被误伤：{by_id['rent_payment']['status']} {by_id['rent_payment']['note']}"
    )


def test_proportional_termination_penalty_not_flagged():
    """「中途解约违约金按未履行部分租金 20%」是合理比例违约金，不构成剥夺退出权."""
    text = (
        "出租方（甲方）：某某置业有限公司，法定代表人：张三，住所：某某市某某区某某路1号。\n"
        "承租方（乙方）：某某科技有限公司。甲方系房屋产权人，持不动产权证，依法出租。\n"
        "租赁期限自2026年10月1日起至2027年9月30日止。月租金1万元，押二付三。\n"
        "租赁期满乙方可续租。装修归乙方所有。维修由甲方负责。\n"
        "乙方中途解约的，违约金按未履行部分租金的百分之二十计算。\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["early_termination"]["status"] == "通过", (
        f"比例违约金被误伤：{by_id['early_termination']['status']} {by_id['early_termination']['note']}"
    )


# ---------- 肉饼终验：subject 经营场所单证必须需关注 ----------

def test_subject_with_only_business_address_flagged():
    """只见「经营场所」不见法定代表人/信用代码：主体信息不完整必须需关注，
    不得因 unless/pass 双表同时含「经营场所」而落「通过」（终验点3）."""
    text = (
        "出租方（甲方）：某某置业有限公司，经营场所：某某市某某区某某路1号。\n"
        "承租方（乙方）：某某科技有限公司。\n"
        "租赁期限自2026年10月1日起至2027年9月30日止。月租金1万元，押二付三。\n"
        "甲方系房屋产权人，依法出租。争议向法院起诉。\n"
        "本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["subject"]["status"] == "需关注", (
        f"只写经营场所必须需关注，实际 {by_id['subject']['status']}：{by_id['subject']['note']}"
    )


# ---------- 肉饼审计 P1：lessor_title 否定盲区 ----------

def test_lessor_title_negated_consent_flagged():
    """「未经产权人同意」不得被 unless 的「产权人同意」子串放行（承租方第一硬伤）."""
    text = (
        "出租方（甲方）：某某贸易有限公司。乙方：某某科技有限公司。\n"
        "本合同系转租，出租方未经产权人同意转租。\n"
        "租赁期限自2026年10月1日起至2027年9月30日止。月租金1万元，押二付三。\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["lessor_title"]["status"] == "需关注", (
        f"未经产权人同意必须需关注，实际 {by_id['lessor_title']['status']}：{by_id['lessor_title']['note']}"
    )


def test_lessor_title_consented_sublease_passes():
    """真授权的转租保持通过（否定直捕规则不得误伤合法授权链条）."""
    text = (
        "出租方（甲方）：某某贸易有限公司，经产权人书面同意对外转租。乙方：某某科技有限公司。\n"
        "原租赁合同仍然有效。租赁期限自2026年10月1日起至2027年9月30日止。\n"
        "月租金1万元，押二付三。争议向法院起诉。本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["lessor_title"]["status"] == "通过", (
        f"经产权人书面同意应通过，实际 {by_id['lessor_title']['status']}：{by_id['lessor_title']['note']}"
    )


# ---------- 肉饼 P3：8 条新禁语的降级链路回归 ----------

def test_lease_new_forbidden_phrase_degrades_scorecard():
    """模型 summary 含租赁新禁语（如「递增条款违法」）→ 重试后仍命中 → 降级为仅展示分数，
    禁语不得回显（机制层已有 test_scorecard 钉死，这里锚定新词表本身）."""
    text = (LEASE / "lease_sample.txt").read_text(encoding="utf-8")
    items = annotate_rule_items(run_checklist(text, "lease")["items"])
    segments = scorecard.load_scorecard_config("lease")["segments"]
    bad_payload = json.dumps(
        {
            "scorecard": {
                "total": 100,
                "summary": "租金递增条款违法，可以放心签署。",
                "segments": [{"key": s["key"], "score": s["weight"]} for s in segments],
            },
            "candidates": [],
        },
        ensure_ascii=False,
    )
    out = run_model_review(
        text=text, items=items, policies=[], category="lease",
        chat_fn=lambda s, u: bad_payload,  # 两轮都输出禁语
    )
    sc = out["scorecard"]
    assert sc.get("degraded") is True, "二次命中禁语必须降级"
    assert "递增条款违法" not in (sc.get("summary") or ""), "禁语不得回显"
