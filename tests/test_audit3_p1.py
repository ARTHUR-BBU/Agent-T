"""外部审计二轮 P1×2 测试：MatchEvidence 证据链 + 定金豁免绑定。

审计基线：「结论正确，证据错误」（风险在第二处、摘句引第一处）+
Codex 反例「定金不予退还；双倍返还货款」被裸词豁免。
"""
from __future__ import annotations

from app.services.clause_index import build_clause_context, build_clause_index
from app.services.checklist import load_checklist, run_checklist


def _sublet_rule() -> dict:
    for item in load_checklist("lease")["items"]:
        if item["id"] == "lessor_title":
            return item
    raise AssertionError("lease 清单必须有 lessor_title 项")


_FAR = "双方就维修责任、水电费用、物业服务等事项另行协商约定，条款以实际履行情况为准补充。" * 8


def _eval(text: str) -> dict:
    from app.services.checklist import _eval_item

    return _eval_item(text, _sublet_rule())


# ---------- P1-1 quote 双向断言（审计指出的测试缺口） ----------

def test_front_safe_back_dangerous_quote_points_to_danger():
    """前安全+后危险：status 需关注 且 摘句必须指向后面的危险 occurrence，
    不得引用前面的保护句（「结论正确、证据错误」的直接断言）。"""
    text = "经产权人书面同意，甲方可将房屋出租。" + _FAR + "本房屋系转租所得，承租人已知悉。"
    out = _eval(text)
    assert out["status"] == "需关注"
    assert "转租所得" in out["quote"], f"摘句必须指向危险 occurrence，实际：{out['quote']}"
    assert "书面同意" not in out["quote"], "摘句不得引用前面的安全保护句"


def test_front_dangerous_back_safe_quote_points_to_danger():
    """前危险+后安全：摘句同样必须指向危险 occurrence（前处）。"""
    text = "本房屋系转租所得，承租人已知悉。" + _FAR + "经产权人书面同意，甲方可将房屋出租。"
    out = _eval(text)
    assert out["status"] == "需关注"
    assert "转租所得" in out["quote"]
    assert "书面同意" not in out["quote"]


def test_no_unless_rule_quote_still_first_match():
    """无 unless 规则：quote = 首个命中（历史行为不变）。"""
    rule = {
        "id": "t", "name": "t", "segment": "A",
        "rules": {"need_attention": [{"any_of": ["没收押金"], "note": "n"}],
                  "pass": [{"any_of": ["正常"]}]},
    }
    from app.services.checklist import _eval_item

    text = "前文提到没收押金的表述。" + "无关填充内容。" * 50 + "后文又见没收押金字样。"
    out = _eval_item(text, rule)
    assert out["status"] == "需关注"
    assert out["quote"].lstrip("…").startswith("前文"), "无 unless 时首个命中即证据"


def test_evidence_coords_flow_through_clause_mapping():
    """evidence 坐标 → primary_clause_id = 证据所在条款（置于 clause_ids 首位）。"""
    text = (
        "总则说明，双方协商一致签订本合同并确认如下条款内容，以资共同遵守执行。\n"
        "第一条 一般约定：双方应遵守违约责任的一般约定并诚信履约。\n"
        "第二条 价款：含税总价拾万元。\n"
        "第三条 交付：按期交付并完成安装调试。\n"
        "第四条 验收：货到七日内验收完毕。\n"
        "第五条 违约责任：甲方未取得产权人同意对外转租的，构成根本违约。\n"
    )
    from app.services.checklist import run_checklist
    from app.services.clause_index import build_clause_index, map_items_to_clauses

    idx = build_clause_index(text)
    assert idx["strategy"] == "numbered", "测试文本应为编号型（正文不得以第N条行首开头）"
    items = run_checklist(text, "lease")["items"]
    map_items_to_clauses(items, idx, text)
    by_id = {i["id"]: i for i in items}
    lt = by_id["lessor_title"]
    assert lt["status"] == "需关注"
    assert lt["primary_clause_id"] == "c06", (
        f"primary 必须是证据所在条款（第五条=c06），实际 {lt['primary_clause_id']}"
    )
    assert lt["clause_ids"][0] == "c06"


# ---------- P1-2 定金豁免绑定（Codex 反例永久测试） ----------

def test_codex_counterexample_double_return_of_payment_still_fires():
    """Codex 反例（二轮 P1-2）：「定金不予退还；双倍返还货款」——双倍返还的
    不是定金，不得豁免，必须仍判需关注。"""
    text = (
        "设备采购合同\n甲方（买方）与乙方（卖方）约定：\n"
        "买方违约时定金不予退还；卖方交付假货时双倍返还货款。\n"
        "货款验收合格后分期支付。\n争议向甲方所在地法院起诉。适用中华人民共和国法律。\n"
        "法定代表人：张三　法定代表人：李四\n"
    )
    items = {i["id"]: i["status"] for i in run_checklist(text, "procurement")["items"]}
    assert items["unfair_terms"] == "需关注", "「双倍返还货款」≠ 定金对称罚则，不得豁免"


def test_true_symmetric_same_sentence_still_exempt():
    """真对称（同句绑定「定金…双倍返还定金」）仍豁免——修复不得矫枉过正。"""
    text = (
        "设备采购合同\n甲方（买方）与乙方（卖方）约定：\n"
        "乙方违约的，已付定金不予退还；甲方违约的，应双倍返还定金。\n"
        "货款验收合格后分期支付。\n争议向甲方所在地法院起诉。适用中华人民共和国法律。\n"
        "法定代表人：张三　法定代表人：李四\n"
    )
    items = {i["id"]: i["status"] for i in run_checklist(text, "procurement")["items"]}
    assert items["unfair_terms"] != "需关注", "真对称定金罚则应豁免"


def test_onesided_forfeit_discriminative_lock_unregressed():
    """既有判别性锁不回退：正序「定金…不予退还」无双倍返还仍直捕。"""
    text = (
        "设备采购合同\n甲方（买方）与乙方（卖方）约定：\n"
        "合同签订后乙方支付定金，若乙方中途解约，定金不予退还。\n"
        "货款验收合格后分期支付。\n争议向甲方所在地法院起诉。适用中华人民共和国法律。\n"
    )
    result = run_checklist(text, "procurement")
    unfair = next(i for i in result["items"] if i["id"] == "unfair_terms")
    assert unfair["status"] == "需关注"
    assert any("定金" in h for h in unfair["hits"])


# ---------- P2 同源修复：ask 上下文 primary 优先 ----------

def test_clause_context_primary_first_not_document_order():
    """同词多条款：真风险条款（primary）在文档后段时，上下文必须以 primary
    开头，不得被前面的同词条款挤出预算（审计二轮 P2）。"""
    parts = []
    for i, num in enumerate("一二三四五六七八", start=1):
        parts.append(f"第{num}条 违约相关：第{i}项通用违约表述，双方按约定承担违约责任并赔偿相应损失。")
    parts.append("第九条 特别违约条款：甲方无正当理由单方解除本合同的，应支付全部合同价款百分之三十的违约金。")
    text = "合同总则说明，双方协商一致签订本合同并确认如下条款，以资共同遵守执行。\n" + "\n".join(parts)
    idx = build_clause_index(text)
    # preamble(c01) + 九条 = 10 档；第九条 = c10
    assert idx["strategy"] == "numbered" and idx["count"] == 10, "测试文本前置校验"
    # primary = 第九条（文档最后），related = 前面 8 条同词条款
    ctx = build_clause_context(
        text, idx,
        [c["id"] for c in idx["clauses"]],
        max_chars=4000,
        primary_clause_id="c10",
    )
    assert ctx.startswith("第九条"), f"上下文必须以 primary 条款开头，实际开头：{ctx[:30]}"
    assert "百分之三十" in ctx, "真风险条款正文必须在上下文中"


def test_clause_context_without_primary_keeps_document_order():
    """无 primary（旧记录/未定位）→ 按文档顺序装填（历史行为）。"""
    text = "第一条 甲\n甲内容。\n第二条 乙\n乙内容。\n第三条 丙\n丙内容。\n"
    idx = build_clause_index(text)
    ctx = build_clause_context(text, idx, ["c01", "c02"])
    assert ctx.startswith("第一条"), "无 primary 时保持文档顺序"
