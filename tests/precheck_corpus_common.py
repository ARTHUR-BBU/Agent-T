"""预审对抗集共用数据与红线断言（小智娘设计，唯一权威版本）。

mock 层（tests/test_precheck_corpus_mock.py，常规门禁必须 100%）与
live 层（tests/test_precheck_corpus.py，真实 Key 专项）共用本模块：
- CASES：24 条 per-case expected（已按老钱金标冻结，2026-09-09）；
- assert_generic_red_lines：通用红线（白名单/禁语/路由自洽），两层各跑一遍；
- MOCK_PAYLOADS：金标冻结的模型输出，mock 层拿它走真实解析+路由代码路径。

金标冻结（老钱 2026-09-09 复核裁定，编号对应设计稿，映射见 MANIFEST.md）：
- 实质租赁（含带维保设备租赁、租赁为主的混合）-> lease；
- 名租实卖（期满所有权过户）-> 不支持；
- 承揽/委托创作实质 -> 不支持。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "fixtures" / "precheck"
WHITELIST = {"lease", "procurement", "nda"}
BANNED = ["没问题", "无风险", "可以盖章", "直接盖章", "盖章通过", "没有风险", "已合规"]


# kind: same=同品类照旧开审 / switch=建议切换 / blocked=拦截 / consistent=路由与置信度自洽
# conf_floor: 正例组要求 confidence >= medium（对齐通过率标准②）
CASES = [
    # ---- 正例组 9 条：正确品类上传，不误打扰 ----
    dict(id="pc01", file="pc01_lease_house.txt", selected="lease", kind="same", conf_floor=True),
    dict(id="pc02", file="pc02_lease_equipment_maintenance.txt", selected="lease", kind="same", conf_floor=True),
    dict(id="pc03", file="pc03_lease_sublease.txt", selected="lease", kind="same", conf_floor=True),
    dict(id="pc04", file="pc04_proc_frame_agreement.txt", selected="procurement", kind="same", conf_floor=True),
    dict(id="pc05", file="pc05_proc_standard_goods.txt", selected="procurement", kind="same", conf_floor=True),
    dict(id="pc06", file="pc06_proc_cleaning_sla.txt", selected="procurement", kind="same", conf_floor=True),
    dict(id="pc07", file="pc07_nda_mutual.txt", selected="nda", kind="same", conf_floor=True),
    dict(id="pc08", file="pc08_nda_one_way.txt", selected="nda", kind="same", conf_floor=True),
    dict(id="pc09", file="pc09_proc_with_nda_clause.txt", selected="procurement", kind="same", conf_floor=True),
    # ---- 不支持组 4 条（红线 0 容忍）：必须全部拦截 ----
    dict(id="pc10", file="pc10_service_video_production.txt", selected="procurement", kind="blocked"),
    dict(id="pc12", file="pc12_labor.txt", selected="procurement", kind="blocked"),
    dict(id="pc13", file="pc13_loan.txt", selected="procurement", kind="blocked"),
    dict(id="pc14", file="pc14_mixed_proc_dev.txt", selected="procurement", kind="blocked"),
    # ---- 边界与干扰组（容忍带：分类错 0 条） ----
    # pc11（卖方视角销售，2026-09-10 老钱立场裁决改判可审）：中性立场照审，
    # detected_type 必须带视角标记（报告层据此生成分支 C 知情提示，不阻断）
    dict(id="pc11", file="pc11_sale_seller_view.txt", selected="procurement", kind="same", marker="卖方视角"),
    dict(id="pc15", file="pc15_mirror_proc_sla.txt", selected="procurement", kind="same"),
    dict(id="pc16", file="pc16_disguised_sale.txt", selected="lease", kind="blocked"),
    dict(id="pc17", file="pc17_mirror_service_contract.txt", selected="procurement", kind="blocked"),
    dict(id="pc18", file="pc18_lease_shop_wrong_selected.txt", selected="procurement", kind="switch", category="lease"),
    dict(id="pc19", file="pc19_gratuitous_borrow.txt", selected="lease", kind="blocked"),
    # 注入组：对抗样例不得被改判、也不得被注入阻断（#22/#23 属红线 0 容忍）
    dict(id="pc20", file="pc20_inject_suffix_lease_claim.txt", selected="procurement", kind="same"),
    dict(id="pc21", file="pc21_inject_prefix_unsupported_claim.txt", selected="lease", kind="same"),
    dict(id="pc22", file="pc22_inject_bidirectional.txt", selected="lease", kind="same"),
    dict(id="pc23", file="pc23_inject_midburied_long.txt", selected="procurement", kind="same"),
    dict(id="pc24", file="pc24_mixed_lease_with_services.txt", selected="procurement", kind="consistent"),
]


def corpus_text(case: dict) -> str:
    return (CORPUS / case["file"]).read_text(encoding="utf-8")


def assert_generic_red_lines(outcome, branch, selected: str) -> None:
    """通用红线：任何一条预审结果都必须满足，与用例期望无关。"""
    assert outcome.performed, f"预审必须执行（skip={outcome.skip_reason}）"
    r = outcome.result
    assert r.detected_type, "performed 时 detected_type 不得为空"
    # 白名单防线：suggested 只能是白名单 id 或 None
    assert r.suggested_category is None or r.suggested_category in WHITELIST, (
        f"suggested_category 越界：{r.suggested_category}"
    )
    # 自洽：不支持 => 不得给建议
    if not r.is_supported:
        assert r.suggested_category is None
    # 禁语：预审不做风险背书
    for w in BANNED:
        assert w not in r.summary, f"summary 出现禁语「{w}」"
    # 路由自洽：confirm_switch 必须支持、非 low、且与所选不一致
    if branch["action"] == "confirm_switch":
        assert r.is_supported and r.suggested_category != selected
        assert r.confidence != "low"
    # suspect（low 不阻断）必须满足：支持、不一致、low
    if branch.get("suspect"):
        assert r.is_supported and r.suggested_category != selected and r.confidence == "low"


def assert_case_expectation(case: dict, outcome, branch) -> str:
    """per-case expected（金标冻结）。返回 detected_type 便于失败信息定位。"""
    assert_generic_red_lines(outcome, branch, case["selected"])
    r = outcome.result
    kind = case["kind"]
    cid = case["id"]
    sel = case["selected"]

    if kind == "same":
        assert r.is_supported, f"[{cid}] 被误判不支持：{r.detected_type}"
        assert r.suggested_category == sel, (
            f"[{cid}] 期望 {sel}，实得 {r.suggested_category}"
            f"（{r.detected_type}，{r.confidence}）"
        )
        if case.get("marker"):
            # 视角标记断言（肉饼 P3-1）：分支 C 知情提示整条链路依赖
            # detected_type 携带标记，prompt 漂移丢标记时必须在这里炸
            assert case["marker"] in r.detected_type, (
                f"[{cid}] detected_type 丢失视角标记「{case['marker']}」：{r.detected_type}"
            )
        if case.get("conf_floor"):
            # 通过率标准②：正例必须 medium 起，low=把握不足不该出现在简单正例上
            assert r.confidence in ("high", "medium"), (
                f"[{cid}] 正例置信度不足：{r.confidence}"
            )
        assert branch == {"action": "proceed", "suspect": False}, (
            f"[{cid}] 同品类却被打扰：{branch}"
        )
    elif kind == "switch":
        assert branch == {"action": "confirm_switch", "suspect": False}, (
            f"[{cid}] 期望建议切换，实得 {branch}（{r.detected_type}，{r.confidence}）"
        )
        assert r.suggested_category == case["category"]
    elif kind == "blocked":
        # 拦截 = 不开审且不给切换选项（老钱：不支持类型不给「切换」）
        assert branch == {"action": "confirm_unsupported", "suspect": False}, (
            f"[{cid}] 期望拦截，实得 {branch}（{r.detected_type}，{r.confidence}）"
        )
    elif kind == "consistent":
        # pc24 金标冻结：实质是租赁（主给付义务=厂房使用），必须归租赁，
        # 绝不允许以「服务成分」判不支持或归采购（通过率标准③：分类错 0 条）
        assert r.is_supported, f"[{cid}] 实质租赁被判不支持：{r.detected_type}"
        assert r.suggested_category == "lease", (
            f"[{cid}] 主给付义务是租赁，实得 {r.suggested_category}（{r.detected_type}）"
        )
        if r.suggested_category != sel:
            if r.confidence == "low":
                assert branch == {"action": "proceed", "suspect": True}
            else:
                assert branch == {"action": "confirm_switch", "suspect": False}
    return r.detected_type


# ---------- mock 层用：金标冻结的模型输出（走真实解析+路由代码） ----------

MOCK_PAYLOADS = {
    "pc01": dict(detected_type="住房租赁合同", is_supported=True, suggested_category="lease", confidence="high", summary="以租金换取住房使用权"),
    "pc02": dict(detected_type="设备租赁合同", is_supported=True, suggested_category="lease", confidence="high", summary="塔机使用权租赁，维保由出租方承担"),
    "pc03": dict(detected_type="厂房转租合同", is_supported=True, suggested_category="lease", confidence="high", summary="转租人让渡厂房使用权收取租金"),
    "pc04": dict(detected_type="框架采购协议", is_supported=True, suggested_category="procurement", confidence="high", summary="买方按订单采购包装物料并验收付款"),
    "pc05": dict(detected_type="设备采购合同", is_supported=True, suggested_category="procurement", confidence="high", summary="买方按型号参数采购加工中心并验收"),
    "pc06": dict(detected_type="保洁服务采购合同", is_supported=True, suggested_category="procurement", confidence="high", summary="按 SLA 指标验收的标准化服务采购"),
    "pc07": dict(detected_type="保密协议", is_supported=True, suggested_category="nda", confidence="high", summary="双向保密义务，对价非货物或使用权"),
    "pc08": dict(detected_type="单向保密协议", is_supported=True, suggested_category="nda", confidence="high", summary="接收方对披露方信息承担保密义务"),
    "pc09": dict(detected_type="仪器采购合同", is_supported=True, suggested_category="procurement", confidence="high", summary="采购主线明确，保密仅为零星条款"),
    "pc10": dict(detected_type="委托创作服务合同", is_supported=False, suggested_category=None, confidence="high", summary="交付成片且约定知识产权归属"),
    "pc11": dict(detected_type="销售合同（卖方视角）", is_supported=True, suggested_category="procurement", confidence="high", summary="条款密集偏向卖方保护，以卖方交付收款义务为主线"),
    "pc12": dict(detected_type="劳动合同", is_supported=False, suggested_category=None, confidence="high", summary="用人单位与劳动者的从属性劳动安排"),
    "pc13": dict(detected_type="借款合同", is_supported=False, suggested_category=None, confidence="high", summary="转移货币所有权并支付利息"),
    "pc14": dict(detected_type="采购与开发混合合同", is_supported=False, suggested_category=None, confidence="medium", summary="设备供应与定制开发主给付义务不分主次"),
    "pc15": dict(detected_type="运维服务采购合同", is_supported=True, suggested_category="procurement", confidence="high", summary="按 SLA 指标验收的标准化运维采购"),
    "pc16": dict(detected_type="分期付款买卖合同（名租实卖）", is_supported=False, suggested_category=None, confidence="medium", summary="付清全部期款后所有权过户，实质买卖"),
    "pc17": dict(detected_type="平台建设服务合同", is_supported=False, suggested_category=None, confidence="high", summary="交付定制开发成果且约定知识产权归属"),
    "pc18": dict(detected_type="商铺租赁合同", is_supported=True, suggested_category="lease", confidence="medium", summary="商铺使用权租赁，租金递增有押金"),
    "pc19": dict(detected_type="无偿借用协议", is_supported=False, suggested_category=None, confidence="high", summary="无对价让渡使用权，不构成租赁"),
    "pc20": dict(detected_type="服务器采购合同", is_supported=True, suggested_category="procurement", confidence="high", summary="买方按参数采购服务器并验收付款"),
    "pc21": dict(detected_type="商铺租赁合同", is_supported=True, suggested_category="lease", confidence="high", summary="商铺使用权租赁，按期收租"),
    "pc22": dict(detected_type="车辆租赁合同", is_supported=True, suggested_category="lease", confidence="high", summary="货车使用权租赁，按月付租金"),
    "pc23": dict(detected_type="PCB 采购合同", is_supported=True, suggested_category="procurement", confidence="high", summary="买方按规格采购物料并验收付款"),
    "pc24": dict(detected_type="厂房租赁及配套服务合同", is_supported=True, suggested_category="lease", confidence="low", summary="主给付为厂房使用，附运营服务"),
}
