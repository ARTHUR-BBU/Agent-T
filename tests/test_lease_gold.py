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
    for iid in ("deposit", "maintenance", "rent_payment", "signature"):
        assert by_id[iid]["status"] == "通过", f"{iid} 应通过，实际 {by_id[iid]['status']}"
    assert by_id["governing_law"]["status"] == "本类不适用"
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


# ---------- 断言 5：同构回归（管辖有/适用法律 na + 仅盖章无签字） ----------

def test_governing_law_and_signature_gold():
    by_id = _items("lease_sample.txt")
    assert by_id["jurisdiction"]["status"] == "通过", "有管辖条款应通过"
    # 老钱裁决一（2026-09-07）：纯境内租赁无选法空间，适用法律转本类不适用
    assert by_id["governing_law"]["status"] == "本类不适用", (
        f"governing_law 应本类不适用，实际 {by_id['governing_law']['status']}"
    )
    assert by_id["signature"]["status"] == "需关注", "仅盖章无签字必须需关注"


# ---------- 断言 6：四连压力测试（≤66） ----------

def test_lease_four_risk_rules_flag():
    by_id = _items("lease_four_risk.txt")
    for iid in ("early_termination", "deposit", "renovation", "maintenance"):
        assert by_id[iid]["status"] == "需关注", f"{iid} 漏报"
    assert by_id["governing_law"]["status"] == "本类不适用"
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


# ---------- 小智娘终验 P1×2：unless 全文域逃逸（引擎已收窄为邻近窗口） ----------

def test_lessor_title_risk_not_washed_by_distant_boilerplate():
    """直捕句「未取得产权人书面同意对外转租」+ 全文远处的转租限制 boilerplate：
    unless 只在正向命中邻近窗口生效，真风险不得被无关条款洗白."""
    text = (
        "出租方（甲方）：某某贸易有限公司。乙方：某某科技有限公司。\n"
        "甲方未取得产权人书面同意对外转租，现出租上述房屋。\n"
        "租赁期限自2026年10月1日起至2027年9月30日止。月租金1万元，押二付三。\n"
        "乙方承租后应合法使用房屋。装修由乙方自行承担费用。维修由甲方负责。\n"
        "未经甲方书面同意，乙方不得擅自转租、转借房屋。\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["lessor_title"]["status"] == "需关注", (
        f"远端 boilerplate 不应洗白真风险：{by_id['lessor_title']['status']} {by_id['lessor_title']['note']}"
    )


def test_early_term_risk_not_washed_by_escalation_clause():
    """目标句「中途解约赔偿未履行租期租金总额」+ 全文远处的「每年递增5%」：
    递增条款的百分号不得经 unless 全文域放空退出权风险."""
    text = (
        "出租方（甲方）：某某置业有限公司，法定代表人：张三，住所：某某市某某区某某路1号。\n"
        "承租方（乙方）：某某科技有限公司。甲方系房屋产权人，持不动产权证，依法出租。\n"
        "租赁期限自2026年10月1日起至2028年9月30日止。租金每年递增5%。\n"
        "月租金首年5万元，押二付三。装修归乙方所有。维修由甲方负责。\n"
        "乙方中途解约的，应赔偿按未履行租期计算的租金总额。\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["early_termination"]["status"] == "需关注", (
        f"递增%不应洗白退出权风险：{by_id['early_termination']['status']} {by_id['early_termination']['note']}"
    )


# ---------- 小智娘终验 P2：完备型主体信息的签署页兜底 ----------

_LEASE_SUBJECT_FALLBACK_TEXT = """租赁合同

出租方（甲方）：某某置业有限公司。承租方（乙方）：某某科技有限公司。

第一条 房屋位于某某区某某路，月租金五万元，用途为办公。
第二条 交付标准为带装修。租赁期限自2026年10月1日起至2028年9月30日止。
本合同项下租金按季度支付，押二付三，乙方应按时支付租金。房屋维修由甲方负责，自然损耗除外。经甲方书面同意乙方可转租。装修归乙方所有，退租无需恢复原状。乙方提前退租的，违约金按未履行部分租金的百分之二十计算。
争议向法院起诉。本合同适用中华人民共和国法律。

落款：
甲方（盖章）：某某置业有限公司
法定代表人（签字）：张三，住所：某某市某某区某某路1号，统一社会信用代码：91110000XXXXXXXXXX
乙方（盖章）：某某科技有限公司
法定代表人（签字）：李四
"""


def test_subject_completion_in_signature_page_passes():
    """头部只写公司名、完整身份信息在签署页（>60 字外）：subject 必须通过，
    不得打出「未见法定代表人」的与事实相反的 note（pass_fulltext_fallback 兜底）."""
    by_id = {
        i["id"]: i
        for i in run_checklist(_LEASE_SUBJECT_FALLBACK_TEXT, "lease")["items"]
    }
    assert by_id["subject"]["status"] == "通过", (
        f"签署页补全应兜底为通过：{by_id['subject']['status']} {by_id['subject']['note']}"
    )


def test_subject_missing_completion_still_flagged():
    """真缺失（全文无法定代表人/信用代码/住所）：完备型兜底不得放过漏报."""
    text = (
        "出租方（甲方）：某某置业有限公司。承租方（乙方）：某某科技有限公司。\n"
        "租赁期限自2026年10月1日起至2027年9月30日止。月租金1万元，押二付三。\n"
        "甲方系房屋产权人，依法出租。争议向法院起诉。\n"
        "本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["subject"]["status"] == "需关注", (
        f"真缺失必须需关注：{by_id['subject']['status']} {by_id['subject']['note']}"
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


# ---------- 回放校准（15 份真实合同，2026-09-07）：词表术语缺口修复 ----------

def test_deposit_refuse_return_margin_flagged():
    """回放 07 漏报修复：「拖欠租金达壹个月…有权拒绝返还保证金」必须需关注."""
    text = (
        "出租方（甲方）：某某置业有限公司。承租方（乙方）：某某科技有限公司。\n"
        "租赁期限自2026年10月1日起至2027年9月30日止。月租金1万元，保证金2万元。\n"
        "乙方如拖欠租金达壹个月，则甲方有权单方终止合同和收回商铺，并有权拒绝返还保证金。\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["deposit"]["status"] == "需关注", (
        f"拒绝返还保证金必须需关注，实际 {by_id['deposit']['status']}"
    )


def test_deposit_confiscate_earnest_margin_flagged():
    """回放 11 漏报修复：「没收壹个月租金的履约保证金」必须需关注."""
    text = (
        "出租方（甲方）：某某置业有限公司。承租方（乙方）：某某科技有限公司。\n"
        "履约保证金按壹个月租金标准收取。\n"
        "乙方无故拖欠租金1个月以上的，甲方有权单方面解除合同，收回房屋，"
        "没收壹个月租金的履约保证金，并追收所欠费用。\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["deposit"]["status"] == "需关注", (
        f"没收履约保证金必须需关注，实际 {by_id['deposit']['status']}"
    )


def test_earnest_margin_benign_refund_passes():
    """防误伤：履约保证金正常退还约定必须保持通过."""
    text = (
        "出租方（甲方）：某某置业有限公司。承租方（乙方）：某某科技有限公司。\n"
        "履约保证金按壹个月租金标准收取，甲方收到保证金后向乙方出具收据。\n"
        "租赁期满，经甲方验收无损坏且无欠款，甲方在乙方办理退房手续时无息退还履约保证金。\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["deposit"]["status"] == "通过", (
        f"保证金正常退还不得误杀，实际 {by_id['deposit']['status']}"
    )


def test_protective_no_refuse_refund_passes():
    """防误伤（肉饼复审 P2-1）：「甲方不得拒绝返还保证金」是承租方保护条款，
    不得被保证金直捕模式误伤（与「有权拒绝返还→需关注」成对）."""
    text = (
        "出租方（甲方）：某某置业有限公司。承租方（乙方）：某某科技有限公司。\n"
        "押金2万元。租赁期满，甲方不得拒绝返还保证金，不得没收押金。\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["deposit"]["status"] == "通过", (
        f"保护性「不得拒绝返还」不得误杀，实际 {by_id['deposit']['status']}"
    )


def test_rent_payment_standard_vocab_not_notfound():
    """回放 08/10 误判修复：「租金标准/支付时间/交纳期限」是有效支付约定，不得未找到."""
    text = (
        "出租方（甲方）：某某置业有限公司。承租方（乙方）：某某科技有限公司。\n"
        "第三条 租金及支付方式。租金标准：每月人民币5000元。\n"
        "支付时间：乙方应于每期首日前5日支付租金至甲方指定账户。\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["rent_payment"]["status"] == "通过", (
        f"标准支付约定词应通过，实际 {by_id['rent_payment']['status']}"
    )


def test_signature_sign_slash_stamp_passes():
    """回放 01 误报修复：示范文本「甲方（签名/盖章）」——签名=签署要件等价词，不得报「仅盖章」."""
    text = (
        "出租方（甲方）：某某置业有限公司。承租方（乙方）：某某科技有限公司。\n"
        "租赁期限自2026年10月1日起至2027年9月30日止。月租金1万元，押二付三。\n"
        "本合同自甲、乙双方签名（盖章）之日起成立并生效。\n"
        "甲方（签名/盖章）：                乙方（签名/盖章）：\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["signature"]["status"] == "通过", (
        f"签名/盖章应通过，实际 {by_id['signature']['status']}：{by_id['signature']['note']}"
    )


def test_signature_stamp_only_still_flagged():
    """回归：仅有盖章、无签字/签名要件时仍必须需关注（F2 修复不得放水）."""
    text = (
        "出租方（甲方）：某某置业有限公司。承租方（乙方）：某某科技有限公司。\n"
        "租赁期限自2026年10月1日起至2027年9月30日止。月租金1万元，押二付三。\n"
        "本合同经双方盖章后生效。甲方（盖章）：某某置业有限公司 乙方（盖章）：某某科技有限公司。\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["signature"]["status"] == "需关注", (
        f"仅盖章必须需关注，实际 {by_id['signature']['status']}"
    )


# ---------- 老钱裁决（2026-09-07）：governing_law 转 na + 指印等价 ----------

def test_governing_law_na_regardless_of_content():
    """纯境内租赁无选法空间（涉外民事关系法律适用法第3条）：适用法律项
    无论合同写不写都落「本类不适用」，不再是常驻噪音（15 份回放 14/15 误报）。"""
    for text in (
        # 写了适用法律的
        "出租方（甲方）：某某置业有限公司。承租方（乙方）：某某科技有限公司。\n"
        "本合同适用中华人民共和国法律。双方签字并加盖公章。",
        # 没写适用法律的
        "出租方（甲方）：某某置业有限公司。承租方（乙方）：某某科技有限公司。\n"
        "争议向房屋所在地人民法院起诉。双方签字并加盖公章。",
    ):
        by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
        assert by_id["governing_law"]["status"] == "本类不适用", (
            f"governing_law 应恒为本类不适用，实际 {by_id['governing_law']['status']}"
        )


def test_signature_fingerprint_equivalent():
    """民法典490条：签名、盖章或按指印三选一等价（老钱裁决二补「按指印/捺印」）."""
    text = (
        "出租方（甲方）：张三。承租方（乙方）：李四。\n"
        "租赁期限自2026年10月1日起至2027年9月30日止。月租金2千元，押一付三。\n"
        "本合同自双方签字并按指印之日起生效。\n"
        "甲方（签字按指印）：            乙方（签字）：\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。"
    )
    by_id = {i["id"]: i for i in run_checklist(text, "lease")["items"]}
    assert by_id["signature"]["status"] == "通过", (
        f"签字+按指印应通过，实际 {by_id['signature']['status']}：{by_id['signature']['note']}"
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
