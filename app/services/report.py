"""M4 审查报告导出：把 store 里的既有审查结果拼装成 docx。

红线（与 M3.5 评分卡架构同源）：
- 报告只做汇总与展示，不产生任何新判断——档位一律照抄规则引擎结果；
- 补盲候选单独成节，必须标注「模型候选，需人工确认」；
- 免责句是必备项：scorecard 没带时由代码补默认句；
- 合同全文不写入报告（只含原文摘句），避免报告本身成为泄露面。
"""
from __future__ import annotations

import io
import re
from typing import Any

from app.services import stance as stance_service
from app.services.scorecard import scrub_forbidden

# 免责句兜底：评分卡未生成（无 Key / 降级）时报告也必须带免责声明
DEFAULT_DISCLAIMER = (
    "本报告由系统自动生成，仅供签约前自查参考，不构成法律意见。"
    "重要合同请在签署前交由律师或法务人员审查。"
)

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_ATTENTION = "需关注"
_NOT_FOUND = "未找到"
_NA = "本类不适用"
# B7-5a（codex 三轮 P2-4）：AI 发现反悔停用档位——历史留痕保留，但**退出
# 当前风险统计**（不算「需关注」也不落「无法识别档位从严处理」）
_WITHDRAWN = "已争议撤回"

# python-docx(lxml) 遇控制字符直接抛错：合同原文经 errors='replace'/pypdf/docling
# 提取后可能残留 \x0b\x0c 等，不清洗则该条审查的报告导出永久 500（肉饼审计 P1）
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize(value: Any) -> Any:
    """递归清洗字符串中的控制字符（单点收口：所有入 docx 的数据都过这里）."""
    if isinstance(value, str):
        return _CONTROL_CHARS.sub("", value)
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


def _scrub(text: Any) -> str:
    """自由文本字段的禁语二次清洗（纵深防御，肉饼/小智娘 M4 审计共同建议）。

    生成层（scorecard/blind_spot）已有一道清洗；报告是印给客户看的文书，
    这里对模型的自由文本字段再过一遍——确定性替换，不是新判断，红线1不受影响。
    """
    return scrub_forbidden(str(text or ""))


def build_report_docx(row: dict[str, Any], warnings: list | None = None) -> bytes:
    """从派生视图拼装报告，返回 docx 字节流。纯展示，无 LLM 调用。

    2c（§4.0）：入参为 derive_claims_view 的派生视图（含 claim/decision/
    一致性标注）——入口必须走统一流水线，禁止原件直进。warnings 为归一化
    迁移警告，本路径捕获但不渲染（迁移警告页面可见；渲染会随每次归一化
    漂移，破坏确定性契约）。
    """
    from docx import Document

    row = _sanitize(row)

    doc = Document()
    _cover(doc, row)
    _conclusion(doc, row)
    _attention_table(doc, row)
    _item_details(doc, row)
    # 章节编号顺延只在草案章节真正渲染时发生（codex #165 复批 P2）：
    # 无修订活动的旧报告保持 M4 原编号（四/五/六），字节契约不破
    has_revision = bool(row.get("revision_section"))
    _revision_section(doc, row)
    _decisions(doc, row, has_revision)
    _ai_opinions(doc, row)
    _ask_thread(doc, row)
    _remediations(doc, row)
    _blind_candidates(doc, row, has_revision)
    _policy_quotes(doc, row, has_revision)
    _appendix(doc, row)
    _footer(doc, row)

    buf = io.BytesIO()
    doc.save(buf)
    return _normalize_docx_zip(buf.getvalue())


def _normalize_docx_zip(data: bytes) -> bytes:
    """重写 ZIP 条目时间戳为固定值（2c §4.3：字节级确定性契约）。

    python-docx 生成的 ZIP 内部时间戳随当前时刻变化，会导致「同档案两次
    导出」偶发字节不同（审计 P1-3 实测）。重写后：同输入 → 恒同字节。
    """
    import zipfile

    src = io.BytesIO(data)
    out = io.BytesIO()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
        out, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for info in zin.infolist():
            fixed = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            fixed.compress_type = info.compress_type
            fixed.external_attr = info.external_attr
            zout.writestr(fixed, zin.read(info.filename))
    return out.getvalue()


# ---------- 各节 ----------

def _cover(doc: Any, row: dict[str, Any]) -> None:
    doc.add_heading("合同审查报告", level=0)
    rows = [
        ("合同文件", row.get("filename") or "未命名"),
        ("合同类型", row.get("category_label") or row.get("category") or ""),
        ("审查时间", row.get("created_at") or "未记录"),
        ("报告编号", row.get("id") or ""),
    ]
    for label, value in rows:
        p = doc.add_paragraph()
        run = p.add_run(f"{label}：")
        run.bold = True
        p.add_run(str(value))

    # 立场声明（阶段 1.3，老钱裁决书）：默认中性也声明——让「按哪把尺子的
    # 哪个方向读」始终摆在明面上；文案由 stance.declaration 单一来源生成
    stance = row.get("stance") or "neutral"
    declaration = stance_service.declaration(row.get("category") or "", stance)
    p = doc.add_paragraph()
    run = p.add_run(f"审查立场：{stance_service.stance_label(row.get('category') or '', stance)}")
    run.bold = True
    doc.add_paragraph(declaration)

    # 品类存疑非阻断提示（老钱金标改判：low 置信度但倾向与所选不一致时
    # 照旧开审，但知情权不能省——报告头必须带上这行）
    pc = row.get("precheck") or {}
    if pc.get("stance_notice"):
        notice = stance_service.counterparty_view_notice(row.get("category") or "")
        if notice:
            p = doc.add_paragraph()
            run = p.add_run(notice)
            run.bold = True
    if pc.get("suspect"):
        p = doc.add_paragraph()
        run = p.add_run(
            f"品类存疑：本报告按「{row.get('category_label') or row.get('category') or '所选品类'}」"
            f"清单审查，AI 预判倾向「{pc.get('detected_type') or '其他类型'}」"
            "（把握较低）。结论请结合文件实际类型阅读。"
        )
        run.bold = True

    # A3 / 九哥：导出范围一句（灰字不抢评分卡；B7-5a 起含 AI 发现与确认记录，
    # 旧记录已存的自带说明按原样渲染——字节契约不破）
    # codex 十六轮 P2：默认文案只在确有对话时扩展「问清楚对话记录」——
    # 无对话旧报告的默认范围说明必须与历史版本逐字节一致
    scope = row.get("export_scope_note") or _default_scope_note(row)
    p = doc.add_paragraph()
    run = p.add_run(scope)
    run.italic = True


def _default_scope_note(row: dict[str, Any]) -> str:
    """默认导出范围说明：基线=历史版本原文（逐字节）；仅当本报告确有
    问清楚对话时插入「、问清楚对话记录」（codex 十六轮 P2-3）。"""
    base = ("报告只含本次读到并展示的内容；未读部分不写入结论"
            "（含规则核查、AI 发现与确认记录")
    if any(
        isinstance(t, dict) and str(t.get("question") or "").strip()
        for t in (row.get("ask_thread_section") or [])
    ):
        base += "、问清楚对话记录"
    if row.get("remediation_records"):
        base += "、补充参考及选用记录"
    if any(
        isinstance(o, dict) and (o.get("title") or o.get("comment"))
        for o in (row.get("ai_finding_opinions") or [])
    ):
        base += "；其中观点部分为 AI 分析，仅供参考"
    return base + "、参考评分与补盲）"


def _known_statuses() -> tuple[str, ...]:
    return ("通过", _ATTENTION, _NOT_FOUND, _NA)


def _conclusion(doc: Any, row: dict[str, Any]) -> None:
    doc.add_heading("一、审查结论汇总", level=1)
    items = row.get("items") or []
    counts = {s: 0 for s in _known_statuses()}
    unknown = 0
    withdrawn = 0
    for it in items:
        status = it.get("status")
        # isinstance 守卫（小智娘 P2-1）：list/dict 等不可哈希脏值走从严分支，
        # 不能在 dict 成员判断上 TypeError 炸 500——fail-closed 而非 fail-loud
        if isinstance(status, str) and status in counts:
            counts[status] += 1
        elif status == _WITHDRAWN:
            # B7-5a（codex 三轮 P2-4）：撤回是历史留痕，退出当前风险统计——
            # 既不进「需关注」也不落「无法识别档位从严处理」
            withdrawn += 1
        else:
            # 未知/变体档位不许静默吞掉（遗留项①，肉饼 P2-2）：计数守恒，
            # 且从严往风险侧倒，绝不落进「全部通过」的错误背书
            unknown += 1
    doc.add_paragraph(
        f"共核查 {len(items)} 项：通过 {counts['通过']} · 需关注 {counts[_ATTENTION]}"
        f" · 未找到 {counts[_NOT_FOUND]} · 本类不适用 {counts[_NA]}"
        + (f" · 已争议撤回 {withdrawn} 项（AI 发现已撤回，历史留痕，不计入风险）"
           if withdrawn else "")
        + (f" · 无法识别档位 {unknown} 项" if unknown else "")
    )
    if unknown:
        doc.add_paragraph("上述无法识别档位已按「需关注」从严处理，请人工复核。")

    sc = row.get("scorecard") or {}
    # 阅读范围（与页面截断旁注一致）
    cov = sc.get("coverage") if isinstance(sc.get("coverage"), dict) else None
    if not cov:
        q = row.get("quality") or {}
        cov = q.get("coverage") if isinstance(q.get("coverage"), dict) else None
    if cov:
        if cov.get("limited"):
            line = "阅读范围：合同较长，本次只读到部分内容，结论供参考"
            try:
                from app.services.evidence import read_clause_label
                extra = read_clause_label(row.get("clause_index"), cov)
                if extra:
                    line = f"{line}（{extra}）"
            except Exception:  # noqa: BLE001
                pass
            doc.add_paragraph(line)
        else:
            doc.add_paragraph("阅读范围：本次已读全文")

    if sc.get("available") and isinstance(sc.get("total"), (int, float)):
        tier = sc.get("tier") or {}
        label = tier.get("label") or ""
        doc.add_paragraph(f"参考评分：{sc['total']}/100（{label}）" if label else f"参考评分：{sc['total']}/100")
        if sc.get("summary"):
            doc.add_paragraph(_scrub(sc["summary"]))
        for cap in sc.get("caps_applied") or []:
            doc.add_paragraph(str(cap), style="List Bullet")
        doc.add_paragraph("评分由模型生成、仅供排序参考，各项结论以逐条规则结果为准。")
    else:
        reason = sc.get("reason") or "未生成"
        doc.add_paragraph(f"参考评分未生成（{reason}），各项结论以逐条规则结果为准。")


def _attention_table(doc: Any, row: dict[str, Any]) -> None:
    doc.add_heading("二、需关注与未找到汇总", level=1)
    known = _known_statuses()
    hard = [
        it for it in (row.get("items") or [])
        if it.get("status") in (_ATTENTION, _NOT_FOUND)
        or (it.get("status") not in known and it.get("status") != _WITHDRAWN)
    ]
    if not hard:
        # 措辞降调（外部审计：关键词初筛系统不应用绝对化表述，防误读为法律结论）
        doc.add_paragraph("无——规则初筛未命中风险项，请以人工复核为准。")
        return
    table = doc.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    header = table.rows[0].cells
    for i, text in enumerate(("条目", "档位", "说明")):
        header[i].text = text
    for it in hard:
        cells = table.add_row().cells
        cells[0].text = it.get("name") or ""
        status = it.get("status") or ""
        # 未知档位进表时显式标注从严口径，不让读者误以为档位可信
        cells[1].text = status if status in (_ATTENTION, _NOT_FOUND) else f"{status or '空'}（按需关注处理）"
        cells[2].text = _scrub(it.get("note"))


def _item_details(doc: Any, row: dict[str, Any]) -> None:
    doc.add_heading("三、逐条明细", level=1)
    shown = [
        it for it in (row.get("items") or [])
        if it.get("status") != _NA
    ]
    if not shown:
        doc.add_paragraph("无适用清单项。")
        return
    for it in shown:
        doc.add_heading(f"【{it.get('status') or '未判定'}】{it.get('name') or ''}", level=2)
        doc.add_paragraph(_scrub(it.get("note")) or "（无说明）")
        # 原文摘句不做禁语清洗——保真是证据义务：清洗合同原文等于篡改证据。
        # 禁语清洗只作用于模型自由文本（note/summary/comment/policy）。
        quote = it.get("quote") or ""
        p = doc.add_paragraph()
        run = p.add_run("原文摘句：")
        run.bold = True
        p.add_run(quote if quote.strip() else "暂无")
        # 2c（§4.1）：主张编号 + 证据状态（未编号/未定位不冒充已核实）
        p = doc.add_paragraph()
        run = p.add_run("主张编号：")
        run.bold = True
        p.add_run(it.get("claim_id") or "未编号（证据不合格，未纳入证据链）")
        p = doc.add_paragraph()
        run = p.add_run("证据状态：")
        run.bold = True
        p.add_run(_evidence_status_text(it.get("evidence")))


def _evidence_status_text(ev: Any) -> str:
    """证据状态中文直陈（§4.1）。红线：不得把「未定位」写成「已核实」。"""
    if not isinstance(ev, dict):
        return "无证据票据"
    verification = ev.get("verification")
    if verification == "verified" and ev.get("evidence_id"):
        return "摘句已核验定位"
    if verification == "ambiguous" and ev.get("evidence_id"):
        return "摘句已定位（多处出现，取首处）"
    if verification in ("missing", "unverified"):
        return "未能定位到原文（该条结论未获原文支撑，请人工核查）"
    return "无证据票据"


# B7-3b 草案章节固定文案（spec-b7-3-ui-report §五A：重审提示任何形态必带；
# 文案待九哥评审后冻结——改文案=改字节，确定性契约测试会拦）
REVISION_REVIEW_NOTICE = (
    "重要提醒：选定修改建议不等于问题已解决——"
    "请把修订后的文本当作新合同重新审查。"
)
REVISION_SECTION_DISCLAIMER = (
    "本节内容由 AI 起草辅助生成，仅供参考；是否采纳由您自行判断，"
    "重要事项请咨询专业律师。"
)


def _revision_entry_impact_lines(entry: dict[str, Any]) -> list[str]:
    """单项牵连检查结果 → 报告行（§五A 四态如实 + 输出检查明示）。"""
    imp = entry.get("impact")
    if imp is None:
        return ["牵连检查：还没有检查过。"]
    if imp.get("legacy"):
        return ["牵连检查：这是一次较早的检查（当时没有记录针对哪个方案），建议重新检查一次。"]
    state = imp.get("state")
    if state == "ok":
        lines: list[str] = ["牵连检查（AI 帮您看这处改动会不会牵动合同别处，不构成法律意见）："]
        # B7-5a：AI 发现项派生词族的如实声明（尽力检索，非品类配置）
        if imp.get("keywords_source") == "derived_from_quote":
            lines.append("（本条为 AI 发现：牵连检索词由发现引句自动派生，属尽力检索，非品类词族配置。）")
        excerpts = imp.get("excerpts") or []
        findings = imp.get("findings") or []
        hidden = imp.get("hidden_findings") or 0
        # 被隐藏说明的**具体编号**（codex #165 复批 P1）：按编号出示对应
        # 合同原文+「说明已隐藏」，不靠「可见数+隐藏数」反推——3 条摘录
        # 只配出 1 条说明的事不能再发生
        hidden_set = {int(x) for x in (imp.get("hidden_indexes") or [])}
        for i, e in enumerate(excerpts):
            f = next((x for x in findings if x.get("index") == i), None)
            excerpt = e.get("excerpt") or ""
            if f is not None:
                lines.append(f"▸ 相关条款 {i + 1}｜摘录：{excerpt}")
                lines.append(f"  说明：{f.get('note') or ''}")
            elif i in hidden_set:
                # 安检隐藏：摘录是合同原文（证据义务原样示出），说明如实
                # 标注已隐藏，不让读者误以为完整
                lines.append(f"▸ 相关条款 {i + 1}｜摘录：{excerpt}")
                lines.append("  说明未通过安全检查，已隐藏。")
            # 其余缺侧=数据不齐：宁缺一条，不错配一条
        if hidden:
            lines.append(
                f"牵连检查结果不完整（{hidden} 条说明未通过安全检查）。")
        if imp.get("notice"):
            lines.append(str(imp["notice"]))
        if not excerpts:
            lines.append("没有找到需要一起查看的其他条款。")
        if any(e.get("excerpt_truncated") for e in excerpts):
            lines.append("说明：相关条款过长，牵连内容已按段摘录送审，"
                         "摘录非完整条款原文。")
        return lines
    if state == "uncovered":
        if imp.get("uncovered_code") == "missing_source":
            return ["牵连检查：合同缺少可对照的完整内容，暂时无法检查。"]
        return ["牵连检查：合同其他部分没有找到与这处改动相关的条款"
                "（不代表没有冲突）。"]
    if state == "degraded":
        return ["牵连检查：这次分析没有完成，请在页面上重试一次。"]
    return [f"牵连检查：状态异常（{state}），请以页面显示为准。"]


def _revision_section(doc: Any, row: dict[str, Any]) -> None:
    """四、AI 修改建议（AI 起草辅助）（B7-3b §五A）。

    数据来自路由层注入的 revision_section（impact.revision_report_section
    纯读库组装，零 LLM）。全卷无修订活动 → 整章不渲染，M4 报告字节
    契约不受影响。红线：禁语过滤已在组装层完成（版别整体处置）；
    合同原文摘录是证据，原样不过滤；建议不回写判定层。"""
    entries = row.get("revision_section") or []
    if not entries:
        return
    doc.add_heading("四、AI 修改建议（AI 起草辅助，不构成法律意见）", level=1)
    doc.add_paragraph(REVISION_SECTION_DISCLAIMER)
    for entry in entries:
        doc.add_heading(
            f"【{entry.get('item_status') or '未判定'}】"
            f"{entry.get('item_name') or ''}", level=2)
        # 原文对照：证据义务，不做禁语清洗（同逐条明细口径）
        p = doc.add_paragraph()
        run = p.add_run("原文摘句：")
        run.bold = True
        p.add_run(entry.get("quote") or "")

        draft = entry.get("draft")
        if draft is None:
            doc.add_paragraph("未选定修改建议。")
        elif draft.get("banned"):
            doc.add_paragraph(
                "您选定的版别未通过安全检查，内容未写入报告。"
                "建议重新生成或另选方案。")
        elif draft.get("missing"):
            doc.add_paragraph("选定记录不完整，内容未写入报告。")
        else:
            p = doc.add_paragraph()
            run = p.add_run("您选定的方案：")
            run.bold = True
            p.add_run(draft.get("stance") or "")
            p = doc.add_paragraph()
            run = p.add_run("修改文本：")
            run.bold = True
            p.add_run(draft.get("text") or "")
            p = doc.add_paragraph()
            run = p.add_run("适用说明：")
            run.bold = True
            p.add_run(draft.get("note") or "")
            p = doc.add_paragraph()
            run = p.add_run("选择理由：")
            run.bold = True
            p.add_run(draft.get("rationale") or "")
            assumptions = draft.get("assumptions") or []
            p = doc.add_paragraph()
            run = p.add_run("事实假设：")
            run.bold = True
            if assumptions:
                p.add_run("；".join(assumptions))
            else:
                p.add_run("无补充假设")
            if draft.get("declaration"):
                doc.add_paragraph(draft["declaration"])

        for line in _revision_entry_impact_lines(entry):
            doc.add_paragraph(line)
        doc.add_paragraph(REVISION_REVIEW_NOTICE)


def _consistency_texts(decision: dict[str, Any]) -> list[str]:
    """决定一致性中文文案（§2.3，reasons 可并存）。"""
    texts = {
        "claim_drift": "主张身份或内容已变化，当前主张与决定时点不一致",
        "evidence_broken": "决定引用的证据票据已失效或无法定位",
    }
    if decision.get("consistency") != "degraded":
        return []
    return [texts.get(r, r) or str(r) for r in decision.get("consistency_reasons") or []]


def _decisions(doc: Any, row: dict[str, Any], has_revision: bool = False) -> None:
    """人工决定与确认（2c §4.2）：有决定或有未编号确认才出现本节。
    编号随草案章节顺延：无=四（M4 原编号）；有=五（B7-3b）。"""
    questions = ((row.get("verify") or {}).get("questions") or [])
    objections = ((row.get("objections") or {}).get("objections") or [])
    decisions: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for q in questions:
        if isinstance(q, dict) and isinstance(q.get("decision"), dict):
            decisions.append((q.get("title") or q.get("question") or "核验问题", q["decision"], q))
    for ob in objections:
        if isinstance(ob, dict) and isinstance(ob.get("decision"), dict):
            decisions.append((ob.get("item_id") or "异议", ob["decision"], ob))
    # 未编号确认披露（v1.5：pending-only 场景的安放处）
    unnumbered = [
        q for q in questions
        if isinstance(q, dict) and q.get("status") in ("confirmed", "disputed")
        and not isinstance(q.get("decision"), dict)
    ]
    # B7-5a：观察类发现（无问题行、无主张编号）的确认/争议记录
    # （ai_finding_records 由报告入口注入，只收 confirmed/disputed）
    obs_records = [
        r for r in (row.get("ai_finding_records") or [])
        if isinstance(r, dict) and r.get("title")
        and r.get("status") in ("confirmed", "disputed")
    ]
    # 受理异议反证状态（§4.1：四态中文直陈）——**不依赖决定存在**（v1.5 钉2：
    # 已受理未采纳的异议也要能看到反证状态）
    accepted = [ob for ob in objections if isinstance(ob, dict) and ob.get("accepted")]
    if not decisions and not unnumbered and not accepted and not obs_records:
        return  # Q4：四者皆无才省略整节
    doc.add_heading("四五"[1 if has_revision else 0] + "、人工决定与确认",
                    level=1)
    if decisions:
        table = doc.add_table(rows=1, cols=6)
        table.style = "Table Grid"
        header = table.rows[0].cells
        for i, text in enumerate(("对象", "决定", "基于主张", "证据引用", "一致性", "决定时间")):
            header[i].text = text
        choice_names = {"confirm": "确认", "dispute": "争议", "adopted": "采纳"}
        for name, d, obj in decisions:
            cells = table.add_row().cells
            cells[0].text = _scrub(name)
            cells[1].text = choice_names.get(d.get("choice") or "", d.get("choice") or "")
            # 钉3（Codex）：指纹实际写入（尾 6 位）——只说不写等于没有审计线索
            _h = d.get("claim_content_hash") or ""
            _claim_cell = d.get("claim_id") or ""
            if _h:
                _claim_cell = _claim_cell + chr(10) + _h[-6:]
            cells[2].text = _claim_cell
            # 证据引用：编号 + 当前定位状态——**只按本对象自己的 evidence_refs
            # 判定**（审计 P1：全局大清单会让「A 的票失效、B 还引用着」串台成
            # 「当前有效」，与一致性栏自相矛盾）
            current_ids = {
                r.get("evidence_id")
                for r in (obj.get("evidence_refs") or []) if isinstance(obj, dict)
            }
            lines = []
            for eid in d.get("evidence_ids") or []:
                state = "已定位（当前有效）" if eid in current_ids else "已失效"
                lines.append(f"{eid}（{state}）")
            cells[3].text = chr(10).join(lines) if lines else "无"
            degraded = _consistency_texts(d)
            cells[4].text = "；".join(degraded) if degraded else "一致"
            cells[5].text = d.get("decided_at") or ""
        doc.add_paragraph(
            "「基于主张」含内容指纹快照尾 6 位（cc- 前缀），用于核对决定时点的主张内容。"
        )
    if unnumbered:
        doc.add_paragraph(
            "以下问题已人工确认，因无主张编号未纳入决定链："
            + "；".join(
                _scrub(q.get("title") or q.get("question") or "")[:40]
                for q in unnumbered
            )
        )
    if obs_records:
        state_names = {"confirmed": "已确认", "disputed": "已争议撤回"}
        doc.add_paragraph(
            "以下 AI 观察发现已人工拍板（观察类无主张编号，未纳入决定链；"
            "确认后已作为【AI 发现】条目列入逐条明细）："
            + "；".join(
                f"【AI 发现】{_scrub(r.get('title') or '')[:40]}"
                f"（{state_names.get(r.get('status') or '', r.get('status') or '')}）"
                for r in obs_records
            )
        )
    # 受理异议反证状态（§4.1：四态中文直陈）
    if accepted:
        doc.add_heading("受理异议的反证状态", level=2)
        state_names = {
            "absent": "模型声明未发现反证（absent）",
            "present": "反证已定位并出具票据（present）",
            "missing": "反证定位失败，未出具票据（missing）",
        }
        for ob in accepted:
            st = ob.get("counter_evidence_status") or ""
            doc.add_paragraph(
                f"{ob.get('item_id') or '异议'}（{ob.get('direction') or ''}）："
                + state_names.get(st, "未走反证资格判定"),
                style="List Bullet",
            )


def _ai_opinions(doc: Any, row: dict[str, Any]) -> None:
    """B7-5a（codex 三轮 P2-5）：观点类发现单列「AI 观点」小节。

    观点无原文引句可核对、不构成清单条目、不可生成修改建议——如实声明
    边界，仅供参考。无观点不渲染整节（旧报告字节契约不破）。
    ai_finding_opinions 由报告入口注入（routes.download_report）。"""
    ops = [
        o for o in (row.get("ai_finding_opinions") or [])
        if isinstance(o, dict) and (o.get("title") or o.get("comment"))
    ]
    if not ops:
        return
    doc.add_heading("AI 观点（仅供参考）", level=1)
    doc.add_paragraph(
        "以下为 AI 的观点性观察，仅供参考；有原文锚点的观点经用户确认后"
        "可列入清单，请结合原文和实际情况自行判断："
    )
    for o in ops:
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(f"【{o.get('dimension_label') or '观点'}】"
                  f"{o.get('title') or ''}").bold = True
        if o.get("comment"):
            doc.add_paragraph(_scrub(o["comment"]), style="List Bullet")


def _remediations(doc: Any, row: dict[str, Any]) -> None:
    """B7-6c（老钱复议 2.3）：补写建议留痕——草稿全文+选用状态进报告。

    铁律口径：选用只是人的表态，不是核查；全程不出现「已补齐/已解决/
    合规」字样。无记录不渲染整节（字节契约不破）。"""
    rems = row.get("remediation_records") or []
    entries = [
        {"item_name": r.get("item_name") or "", **r}
        for r in rems if isinstance(r, dict) and (
            r.get("mode") == "draft" or r.get("mode") == "elsewhere"
            or r.get("mode") == "guidance")
    ]
    if not entries:
        return
    doc.add_heading("补充参考记录（示范文本，系统未验证）", level=1)
    doc.add_paragraph(
        "以下为「未找到」条目的补写参考留痕。AI 生成的示范文本仅供参考，"
        "不构成法律意见；采用前请专业人士复核。选用标记只是您的表态，"
        "本系统未验证实际修改；建议重新提交审查以确认。"
    )
    mode_names = {"draft": "补充草稿", "elsewhere": "可能相关的合同内容（请核对）",
                  "guidance": "行动指引（该类内容不宜由 AI 出示范文本）"}
    for e in entries:
        p = doc.add_paragraph()
        run = p.add_run(f"{e.get('item_name') or '未找到条目'}｜"
                        f"{mode_names.get(e.get('mode') or '', e.get('mode') or '')}")
        run.bold = True
        if e.get("mode") == "guidance":
            doc.add_paragraph(_scrub(e.get("guidance")) or "（无）", style="List Bullet")
            continue
        ew = e.get("elsewhere") or {}
        if e.get("mode") == "elsewhere" and ew.get("found"):
            doc.add_paragraph(f"AI 说明：{_scrub(ew.get('summary')) or '（无）'}",
                              style="List Bullet")
            for q in (ew.get("quotes") or [])[:3]:
                doc.add_paragraph(f"原文摘句：{q}", style="List Bullet")
        dr = e.get("draft") or {}
        if e.get("mode") == "draft":
            original = e.get("initial_draft") or {}
            if e.get("user_edited") and isinstance(original, dict) and original.get("clause_draft"):
                doc.add_paragraph("AI 最初示范文本：", style="List Bullet")
                doc.add_paragraph(_scrub(original["clause_draft"]))
            for previous in e.get("past_adoptions") or []:
                if isinstance(previous, dict) and previous.get("clause_draft"):
                    doc.add_paragraph("此前选用文本（后经用户调整，系统未验证）：", style="List Bullet")
                    doc.add_paragraph(_sanitize(str(previous["clause_draft"])))
            if dr.get("clause_draft"):
                doc.add_paragraph("用户调整后的文本：" if e.get("user_edited") else "草稿正文：",
                                  style="List Bullet")
                doc.add_paragraph(_sanitize(str(dr["clause_draft"])) if e.get("user_edited")
                                  else _scrub(dr["clause_draft"]))
            if dr.get("placement"):
                doc.add_paragraph(f"常见位置参考：{_scrub(dr['placement'])}",
                                  style="List Bullet")
            if dr.get("blanks"):
                doc.add_paragraph("留空待填：" + "、".join(dr["blanks"]),
                                  style="List Bullet")
        adopted = "用户已选用（系统未验证）" if e.get("adopted") else "未选用"
        doc.add_paragraph(f"选用状态：{adopted}", style="List Bullet")


def _ask_thread(doc: Any, row: dict[str, Any]) -> None:
    """B7-6（codex 十五轮 P1-1，计划 §三-补3）：问清楚对话记录入报告。

    每轮含所属条目 + 问答四段式摘要；标注「辅助思考过程，非法律意见」。
    无对话不渲染整节（B7-3b 教训：旧报告字节契约不破）。
    ask_thread_section 由报告入口注入（routes.download_report）。"""
    turns = [
        t for t in (row.get("ask_thread_section") or [])
        if isinstance(t, dict) and str(t.get("question") or "").strip()
    ]
    if not turns:
        return
    doc.add_heading("问清楚对话记录（辅助思考过程，非法律意见）", level=1)
    doc.add_paragraph(
        "以下是您在本次审查中与 AI 的追问对话，随审查单一并保存。"
        "对话内容是辅助思考过程，不构成法律意见；结论仍以清单核查与人工判断为准。"
    )
    seg_labels = (("原文依据", "原文在哪"), ("实际影响", "问题是啥"),
                  ("建议改法", "建议怎么改"))
    for t in turns:
        name = t.get("item_name") or "通用追问"
        p = doc.add_paragraph()
        suffix = "（AI 观点）" if t.get("source") == "opinion" else (
            " · 补写" if t.get("source") == "remediate" else "")
        run = p.add_run(f"关于：{name}{suffix}")
        run.bold = True
        doc.add_paragraph(f"问：{t.get('question')}", style="List Bullet")
        state = t.get("state") or ""
        if state == "success" and isinstance(t.get("answer"), dict):
            ans = t["answer"]
            for label, key in seg_labels:
                val = str(ans.get(key) or "").strip()
                if not val:
                    continue
                sp = doc.add_paragraph(style="List Bullet")
                sr = sp.add_run(f"{label}：")
                sr.bold = True
                sp.add_run(_scrub(val))
            rewrite = str(ans.get("改写稿") or "").strip()
            if rewrite and ans.get("quote_verified"):
                rp = doc.add_paragraph(style="List Bullet")
                rr = rp.add_run("建议改写稿：")
                rr.bold = True
                rp.add_run(_scrub(rewrite))
                doc.add_paragraph("（复制前先对照原文核一遍，确认意思没跑偏）")
        elif state == "failed":
            err = str(t.get("error") or "").strip()
            doc.add_paragraph(f"答：（本次未答上{'：' + _scrub(err) if err else ''}）",
                              style="List Bullet")


def _blind_candidates(doc: Any, row: dict[str, Any],
                      has_revision: bool = False) -> None:
    candidates = row.get("blind_candidates") or []
    if not candidates:
        return
    doc.add_heading("五六"[1 if has_revision else 0] + "、模型补盲候选（需人工确认）",
                    level=1)
    doc.add_paragraph(
        "以下为模型提出的候选风险，未经规则引擎确认，不构成审查结论，"
        "请人工核实后再决定是否采信。"
    )
    for it in candidates:
        name = it.get("name") or ""
        marker = "· 来自评分卡点名" if it.get("named_by_scorecard") else ""
        doc.add_heading(f"候选｜{name}{marker}", level=2)
        doc.add_paragraph(_scrub(it.get("note")) or "（无说明）")
        quote = it.get("quote") or ""
        p = doc.add_paragraph()
        run = p.add_run("原文摘句：")
        run.bold = True
        p.add_run(quote if quote.strip() else "暂无")


def _policy_quotes(doc: Any, row: dict[str, Any],
                   has_revision: bool = False) -> None:
    doc.add_heading("六七"[1 if has_revision else 0] + "、政策摘句", level=1)
    policies = [p for p in (row.get("policies") or []) if str(p).strip()]
    if not policies:
        doc.add_paragraph("本次审查未引用政策条款。")
        return
    for policy in policies:
        doc.add_paragraph(_scrub(policy), style="List Bullet")


def _appendix(doc: Any, row: dict[str, Any]) -> None:
    doc.add_heading("附注", level=1)
    na_names = [
        it.get("name") or "" for it in (row.get("items") or [])
        if it.get("status") == _NA
    ]
    if na_names:
        doc.add_paragraph("本类不适用（未核查）：" + "、".join(n for n in na_names if n))
    skips = [s for s in (row.get("blind_skipped_messages") or []) if str(s).strip()]
    if skips:
        doc.add_paragraph("补盲候选跳过：" + "；".join(str(s) for s in skips))


def _footer(doc: Any, row: dict[str, Any]) -> None:
    sc = row.get("scorecard") or {}
    disclaimer = _scrub(sc.get("disclaimer")).strip() or DEFAULT_DISCLAIMER
    doc.add_paragraph()
    p = doc.add_paragraph()
    run = p.add_run(disclaimer)
    run.italic = True
    # 页尾范围句：历史版本默认无括注——保持逐字节原样（codex 十六轮 P2-3）
    scope = row.get("export_scope_note") or (
        "报告只含本次读到并展示的内容；未读部分不写入结论"
    )
    p2 = doc.add_paragraph()
    run2 = p2.add_run(scope)
    run2.italic = True
    doc.add_paragraph(f"报告编号 {row.get('id') or ''} · 由合同审查 Agent 自动生成")
