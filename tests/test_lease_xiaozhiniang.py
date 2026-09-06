"""小智娘 · 租赁品类对抗与回归测试（feat/lease-category, HEAD 0365340）。

分工说明：tests/test_lease_gold.py 钉老钱 §六金标；本文件做对抗面——
1. 换措辞对抗（学 test_procurement_paraphrase.py）：同义改写不得漏报
2. missing_as 加严项回归（lessor_title/lease_term/governing_law/signature）
3. 误报扫描：正常到无聊的租赁合同，需关注 ≤3 且每条讲得出理由
4. 禁语误伤评估：8 条新禁语全品类子串匹配，验证不误触 + 真禁语能拦 + 否定句误伤观察
5. API 层：lease 品类走通 upload/review/report，封面品类显示「租赁合同」
6. 权重/段位：D=20、合计 100、无 NA 段

无网络、无 Key；fixtures 以内联字符串维护（不新增 fixtures/ 文件）。
标注【已知缺口】的断言是现状钉死（characterization）：开发狗补词表后必须把
对应断言改成「需关注」并更新本注释，不得直接删除。
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.main import app
from app.services import llm_ask, scorecard
from app.services.blind_spot import annotate_rule_items
from app.services.checklist import run_checklist
from app.services.model_review import run_model_review

ROOT = Path(__file__).resolve().parents[1]

client = TestClient(app)

D_SEGMENT_ITEMS = ("early_termination", "deposit", "renovation")
TIGHTENED_MISSING = ("lessor_title", "lease_term", "governing_law", "signature")


# ---------- 内联 fixtures ----------

# 良性头部：主体完整 + 出租权限自证，让后续断言聚焦风险条款本身
_BENIGN_HEAD = """房屋租赁合同

出租方（甲方）：某某置业有限公司，法定代表人：张三，住所：某某市某某区某某路1号，统一社会信用代码：91110000MA01AB2C3D。
承租方（乙方）：某某科技有限公司，法定代表人：李四，住所：某某市某某区某某路2号。
甲方系上述房屋产权人，持不动产权证，依法出租。

租赁期限自2026年10月1日起至2028年9月30日止；期满乙方可续租。
交付标准：带装修交付，消防验收合格，双方签署交接单。房屋用途为办公，甲方配合办理备案。
月租金为5万元，押二付三。免租期30天。
经甲方书面同意，乙方可转租。
"""
_BENIGN_TAIL = """
因本合同发生争议，向房屋所在地人民法院起诉。本合同适用中华人民共和国法律。
本合同一式两份，经双方签字并加盖公章后生效。
"""

# 对抗 fixture A：四连风险的同义改写（保留法律术语、重写句式）——规则层必须仍标需关注
LEASE_PARAPHRASE = _BENIGN_HEAD + """
乙方在租赁期限届满前解约的，剩余租期对应的租金作为违约金赔付甲方。
押金作为违约金全部扣除，乙方无权要求返还。
租赁物的日常维护、保养及维修责任概由乙方负责。
租赁期满，乙方应将房屋恢复原样，由此产生的费用由乙方自行承担。
""" + _BENIGN_TAIL

# 对抗 fixture B：进一步去关键词化的改写——现状抓不到，钉死缺口防回归恶化
LEASE_PARAPHRASE_HARD = _BENIGN_HEAD + """
乙方中途解约的，应赔偿按未履行租期计算的租金总额。
押金在发生任何违约情形时全额扣除，乙方无权要求返还。
房屋及设施的维修保养由乙方自行负责并承担相关费用。
合同终止后，乙方应自负费用将房屋恢复原状。
""" + _BENIGN_TAIL

# 极简合同：只有一句话，四个 missing_as=需关注 加严项必须全部落需关注
LEASE_MINIMAL = "房屋租赁合同\n\n甲乙双方经协商一致，就房屋租赁事宜达成如下协议。"

# 正常到无聊的合同：装修与维修各归其位（装修句与维修句相邻，真实合同常见写法）
LEASE_BORING = _BENIGN_HEAD + """
装修归乙方所有。房屋维修由甲方负责，自然损耗除外。
乙方提前退租的，应提前三十日书面通知甲方，并按一个月租金支付违约金。
""" + _BENIGN_TAIL

# 押金盲区观察文本：有押金、全文无退还时限（老钱意见书 §六.3 / §八.1）
LEASE_DEPOSIT_BLIND = LEASE_BORING.replace(
    "月租金为5万元，押二付三。免租期30天。",
    "月租金为5万元，押二付三。免租期30天。乙方于签约时向甲方支付租赁押金6万元，用于担保本合同履行。",
)


def _by_id(text: str) -> dict[str, dict]:
    result = run_checklist(text, "lease")
    return {it["id"]: it for it in result["items"]}


def _attention_ids(text: str) -> list[str]:
    return [it["id"] for it in run_checklist(text, "lease")["items"] if it["status"] == "需关注"]


def _model_full_marks() -> dict:
    segments = scorecard.load_scorecard_config("lease")["segments"]
    return {
        "scorecard": {
            "total": 100,
            "summary": "条款完备，可以放心签署。",
            "segments": [{"key": s["key"], "score": s["weight"]} for s in segments],
        },
        "candidates": [],
    }


def _capped_total(text: str) -> int:
    """模型满分 payload + 代码重算/封顶后的总分（同 test_lease_gold._capped_total 思路）。"""
    items = annotate_rule_items(run_checklist(text, "lease")["items"])
    hard_names = "、".join(
        i["name"] for i in items if i["status"] != "通过" and not i.get("category_na")
    )
    payload = _model_full_marks()
    payload["scorecard"]["segments"][0]["comment"] = "点名：" + hard_names
    out = run_model_review(
        text=text, items=items, policies=[], category="lease",
        chat_fn=lambda s, u: json.dumps(payload, ensure_ascii=False),
    )
    assert out["scorecard"]["available"] is True
    return out["scorecard"]["total"]


# ================= 1. 换措辞对抗 =================

@pytest.mark.parametrize(
    "item_id", ["early_termination", "deposit", "maintenance", "renovation"]
)
def test_paraphrase_four_risks_still_attention(item_id):
    """同义改写（保留法律术语、重写句式）不得漏报，四连风险必须仍是需关注。"""
    item = _by_id(LEASE_PARAPHRASE)[item_id]
    assert item["status"] == "需关注", (
        f"{item_id} 同义改写后漏报（实际 {item['status']}）：{item.get('note')}"
    )


def test_paraphrase_adversarial_wording_absent():
    """对抗 fixture 不得残留原版关键词（否则对抗强度不够）。"""
    assert "剩余租期全部租金" not in LEASE_PARAPHRASE
    assert "押金不予退还" not in LEASE_PARAPHRASE
    assert "一切维修均由乙方承担" not in LEASE_PARAPHRASE
    assert "恢复原状" not in LEASE_PARAPHRASE


# ---- 已知缺口（现状钉死）：进一步去关键词化的改写，规则层抓不到 ----

def test_known_gap_early_term_synonym_misses():
    """【P1 漏报已修，2026-09-06 转正】「中途解约/未履行租期租金总额」改写
    由 early_termination 新增同义改写直捕规则命中 → 需关注。"""
    item = _by_id(LEASE_PARAPHRASE_HARD)["early_termination"]
    assert item["status"] == "需关注", f"改写应命中需关注：{item['status']} {item['note']}"


def test_known_gap_deposit_forfeit_synonym_washes_out():
    """【P1 漏报已修，转正】「押金全额扣除/无权要求返还」由 deposit 新增
    全额扣除/无权要求返还模式命中 → 需关注（不再被 pass 词表「押金」洗白）。"""
    item = _by_id(LEASE_PARAPHRASE_HARD)["deposit"]
    assert item["status"] == "需关注", f"洗白应堵住：{item['status']} {item['note']}"


def test_known_gap_renovation_cost_order_swap_washes_out():
    """【P1 漏报已修，转正】「自负费用…恢复原状」费用前置语序由 renovation
    新增语序变体模式命中 → 需关注。"""
    item = _by_id(LEASE_PARAPHRASE_HARD)["renovation"]
    assert item["status"] == "需关注", f"语序变体应命中：{item['status']} {item['note']}"


def test_known_gap_maintenance_baoyang_synonym_misses():
    """【已知缺口 P2 漏报】「维修保养由乙方自行负责」不含 均由/概由/全部由 等
    强归属词 → 未找到（漏抓但未洗白，靠 missing 兜底）。补词表后应改为「需关注」。"""
    item = _by_id(LEASE_PARAPHRASE_HARD)["maintenance"]
    assert item["status"] == "未找到", f"现状钉死失效，行为已变化：{item['status']}"


def test_hard_paraphrase_cap_still_holds_via_not_found():
    """硬改写四连下，漏报的两项被「未找到」兜底网接住（未找到同样计入封顶），
    模型谎报满分仍应被压进 ≤74——漏报不再恶化成洗分。"""
    by_id = _by_id(LEASE_PARAPHRASE_HARD)
    hard = [i for i in D_SEGMENT_ITEMS if by_id[i]["status"] != "通过"]
    assert hard, "D 段必须仍有靶点，否则封顶完全失效"
    assert _capped_total(LEASE_PARAPHRASE_HARD) <= 74


# ================= 2. missing_as 加严项回归 =================

def test_minimal_contract_tightened_items_attention():
    """极简合同下 4 个 missing_as=需关注 加严项必须全部落需关注
    （lessor_title 是刻意加严：出租权限无法从文本自证）。"""
    by_id = _by_id(LEASE_MINIMAL)
    for iid in TIGHTENED_MISSING:
        assert by_id[iid]["status"] == "需关注", (
            f"{iid} 缺失时应落「需关注」（missing_as 加严），实际 {by_id[iid]['status']}"
        )
    assert by_id["lessor_title"]["note"], "加严项缺失时必须带 missing_note 说明"


def test_minimal_contract_default_items_not_found():
    """未配置 missing_as 的其余项按默认档位落「未找到」，不冒充加严项。"""
    statuses = {it["id"]: it["status"] for it in run_checklist(LEASE_MINIMAL, "lease")["items"]}
    for iid, status in statuses.items():
        if iid in TIGHTENED_MISSING:
            continue
        assert status == "未找到", f"{iid} 在极简合同下应默认「未找到」，实际 {status}"
    assert sum(1 for s in statuses.values() if s == "需关注") == 4, (
        "需关注项必须恰好是 4 个加严项，不得多（误报）也不得少（加严失效）"
    )


# ================= 3. 误报扫描 =================

def test_boring_contract_attention_within_budget():
    """正常到无聊的合同：需关注 ≤3，且每条都能讲出理由（见下方钉死测试）。"""
    flagged = _attention_ids(LEASE_BORING)
    assert len(flagged) <= 3, f"无聊合同误报超预算：{flagged}"
    # 预算内每一项都必须有解释性 note（不能裸标）
    by_id = _by_id(LEASE_BORING)
    for iid in flagged:
        assert by_id[iid]["note"], f"{iid} 被标需关注但无 note，用户无从判断理由"


def test_boring_contract_clean_variant_zero_attention():
    """装修句与维修句拉开距离的等价无聊合同：0 需关注——证明规则层有能力全绿，
    也反衬下方跨句误报测试钉住的是一个窗口宽度问题，不是「租赁必报」设计。"""
    spaced = LEASE_BORING.replace(
        "装修归乙方所有。房屋维修由甲方负责，自然损耗除外。",
        "装修归乙方所有，退租时双方另行协商处置。\n房屋维修由甲方负责，自然损耗除外。",
    )
    assert _attention_ids(spaced) == [], "全绿能力丢失：无聊合同出现误报"


def test_known_gap_renovation_cross_clause_false_positive():
    """【P1 误报已修，转正】装修归属模式加句界否定（[^，。；！？\n]），
    「装修归乙方所有。房屋维修由甲方负责」不再跨句误配 → 通过。"""
    item = _by_id(LEASE_BORING)["renovation"]
    assert item["status"] == "通过", f"跨句误报应已修复：{item['status']} {item['note']}"


def test_deposit_blind_spot_no_refund_deadline_passes():
    """观察点（非 xfail，与老钱 §六.3 口径一致）：有押金、全文无退还时限 → 现状落
    「通过」。这是关键词规则已知盲区（押金词命中 pass 词表即过，退还时限未参与
    判定），不是本分支引入的回归；开发狗用 all_of（押金词+退还时限词）细化后，
    此处应改断言为「需关注」并删除本注释。"""
    item = _by_id(LEASE_DEPOSIT_BLIND)["deposit"]
    assert item["status"] == "通过", f"盲区行为已变化，请更新本观察点：{item['status']}"
    # 盲区文本除此之外不得引入其他误报
    extra = [i for i in _attention_ids(LEASE_DEPOSIT_BLIND) if i != "renovation"]
    assert extra == [], f"押金盲区文本出现额外误报：{extra}"


# ================= 4. 禁语误伤评估 =================
# 8 条新禁语（config/scorecard_forbidden.yaml category_a）是全品类生效的子串匹配。

BENIGN_LEASE_TALK = [
    # 老钱 §七.1/2/3/4 允许的合规话术，不得被新禁语误触
    "押金应于租赁期满后七日内无息退还。",
    "双方约定的租金递增机制合法有效，建议明确递增上限与计算方式。",
    "租金递增幅度与机制不明确，建议谈定上限。",
    "租赁期限超过二十年的部分不受法律保护，建议拆分续签。",
    "该条款存在被认定无效的风险，请核验产权与建设手续。",
    "押金退还条件目前依赖出租方单方确认，退还时点不可控。",
]


@pytest.mark.parametrize("sentence", BENIGN_LEASE_TALK)
def test_new_forbidden_words_do_not_hit_benign_talk(sentence):
    assert scorecard.check_forbidden(sentence) == [], (
        f"新禁语误伤合规话术：「{sentence}」"
    )


NEW_FORBIDDEN_WORDS = [
    "递增条款违法", "递增条款无效", "递增约定无效",
    "所以合同无效", "这份合同无效",
    "押金肯定能退", "押金一定能退", "到期不会赶你走",
]


@pytest.mark.parametrize("word", NEW_FORBIDDEN_WORDS)
def test_new_forbidden_words_are_loaded_and_scrubbed(word):
    """8 条新禁语全部进评分卡清单，且清洗器拦得住、留痕、幂等。"""
    assert word in scorecard.check_forbidden(f"模型说：{word}。"), f"新禁语未生效：{word}"
    once = scorecard.scrub_forbidden(word)
    assert "【已过滤】" in once, f"清洗未留痕：{word}"
    assert scorecard.scrub_forbidden(once) == once, f"清洗不幂等：{word}"


def test_forbidden_two_layers_union_no_escape_lease():
    """纵深防御（照 test_forbidden_two_layers_union_no_escape 的 union 语义）：
    租赁背书句要么被评分卡禁语拦，要么被追问禁语拦，两层不得同时放行。"""
    lease_backing = ["押金肯定能退回来", "递增条款违法所以不能签", "到期不会赶你走"]
    for s in lease_backing:
        caught = bool(scorecard.check_forbidden(s)) or (
            llm_ask._scrub_banned_echo(s) != s
        )
        assert caught, f"租赁背书句两层防线都放行：「{s}」"


def test_forbidden_negation_false_positive_observed():
    """【P2 误伤已修，转正】check_forbidden 升级为否定感知：命中词紧邻前缀
    （≤4 字）为否定表述时不算违规——正当风险提示话术不再被反向清洗。
    真禁语裸命中不受影响（其余禁语测试钉死）。"""
    for sentence in (
        "并不能说明这份合同无效",
        "不能认定押金一定能退",
        "不存在递增条款违法的情形",
    ):
        hits = scorecard.check_forbidden(sentence)
        assert hits == [], f"否定句仍被误伤：{sentence} → {hits}"
        assert scorecard.scrub_forbidden(sentence) == sentence, f"否定句被清洗：{sentence}"


# ================= 5. API 层 =================

def test_api_upload_lease_review_report():
    """lease 品类走通 upload → review → report 全链路，封面品类显示「租赁合同」。"""
    files = {"file": ("lease_sample.txt", (ROOT / "fixtures" / "lease_sample.txt").read_bytes(), "text/plain")}
    r = client.post("/api/upload", files=files, data={"category": "lease"})
    assert r.status_code == 200
    rid = r.json()["review_id"]

    body = client.get(f"/api/review/{rid}").json()
    assert body["status"] == "done"
    assert body["category"] == "lease"
    assert body["category_label"] == "租赁合同", "报告封面/列表品类名必须来自 checklist 配置"
    assert any(i["status"] == "需关注" for i in body["items"]), "lease 样例应至少有一条需关注"
    assert body["scorecard"]["available"] is False, "无 Key 环境评分卡必须明确未开通而非缺失"

    r3 = client.get(f"/api/review/{rid}/report")
    assert r3.status_code == 200
    doc = Document(io.BytesIO(r3.content))
    text = "\n".join(p.text for p in doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                text += "\n" + cell.text
    assert "合同审查报告" in text
    assert "租赁合同" in text, "报告封面必须显示「租赁合同」"


def test_api_lease_with_mocked_llm_scorecard(monkeypatch):
    """monkeypatch 模型通道：lease 评分卡（D=20 七段）经 API 原样透传，报告带分。"""
    from app.graph import pipeline

    segments = scorecard.load_scorecard_config("lease")["segments"]
    sc = {
        "available": True, "reason": None, "total": 68,
        "tier": {"label": "有实质风险", "hint": "先改完再谈签的事"},
        "summary": "规则结果汇总评分 68 分。",
        "segments": [
            {"key": s["key"], "name": s["name"], "weight": s["weight"],
             "score": s["weight"] - 2, "comment": "", "na": False}
            for s in segments
        ],
        "caps_applied": [], "disclaimer": "机器参考意见。", "advisory_only": True,
    }

    def fake_review(**kwargs):
        return {
            "scorecard": sc, "blind_candidates": [], "blind_skipped_messages": [],
            "blind_skipped_reason": None, "blind_enabled": True,
        }

    monkeypatch.setattr(pipeline, "run_model_review", fake_review)
    files = {"file": ("lease_sample.txt", (ROOT / "fixtures" / "lease_sample.txt").read_bytes(), "text/plain")}
    rid = client.post("/api/upload", files=files, data={"category": "lease"}).json()["review_id"]
    body = client.get(f"/api/review/{rid}").json()

    assert body["scorecard"]["available"] is True
    assert body["scorecard"]["total"] == 68
    assert [s["key"] for s in body["scorecard"]["segments"]] == ["A", "B", "C", "D", "E", "F", "G"]

    r = client.get(f"/api/review/{rid}/report")
    assert r.status_code == 200
    text = "\n".join(p.text for p in Document(io.BytesIO(r.content)).paragraphs)
    assert "参考评分：68/100（有实质风险）" in text
    assert "租赁合同" in text


# ================= 6. 权重/段位 =================

def test_lease_scorecard_d_weight_20_and_no_na_segment():
    segments = scorecard.load_scorecard_config("lease")["segments"]
    by_key = {s["key"]: s for s in segments}
    assert by_key["D"]["weight"] == 20, "租赁 D 段必须比采购（16）重，法务定为 20"
    assert sum(s["weight"] for s in segments) == 100
    assert all(s["na"] is False for s in segments), "租赁七段全部适用，不得有 NA 段"


def test_lease_all_items_mapped_to_segments():
    """老钱 L-1：每个 item 必须落到已定义分段（load 时已 fail-fast，这里再钉一层语义）。"""
    cfg_items = run_checklist(LEASE_BORING, "lease")["items"]
    valid = {"A", "B", "C", "D", "E", "F", "G"}
    assert len(cfg_items) == 14
    for it in cfg_items:
        assert it["segment"] in valid, f"{it['id']} 的 segment {it['segment']!r} 不在七段内"
    assert not any(it["category_na"] for it in cfg_items), "租赁无 NA 项"
