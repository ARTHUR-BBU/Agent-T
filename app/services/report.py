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
    _decisions(doc, row)
    _blind_candidates(doc, row)
    _policy_quotes(doc, row)
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

    # A3 / 九哥：导出范围一句（灰字不抢评分卡）
    scope = row.get("export_scope_note") or (
        "报告只含本次读到并展示的内容；未读部分不写入结论"
        "（含规则核查、参考评分与补盲；不含页面 AI 观察及追问）"
    )
    p = doc.add_paragraph()
    run = p.add_run(scope)
    run.italic = True


def _known_statuses() -> tuple[str, ...]:
    return ("通过", _ATTENTION, _NOT_FOUND, _NA)


def _conclusion(doc: Any, row: dict[str, Any]) -> None:
    doc.add_heading("一、审查结论汇总", level=1)
    items = row.get("items") or []
    counts = {s: 0 for s in _known_statuses()}
    unknown = 0
    for it in items:
        status = it.get("status")
        # isinstance 守卫（小智娘 P2-1）：list/dict 等不可哈希脏值走从严分支，
        # 不能在 dict 成员判断上 TypeError 炸 500——fail-closed 而非 fail-loud
        if isinstance(status, str) and status in counts:
            counts[status] += 1
        else:
            # 未知/变体档位不许静默吞掉（遗留项①，肉饼 P2-2）：计数守恒，
            # 且从严往风险侧倒，绝不落进「全部通过」的错误背书
            unknown += 1
    doc.add_paragraph(
        f"共核查 {len(items)} 项：通过 {counts['通过']} · 需关注 {counts[_ATTENTION]}"
        f" · 未找到 {counts[_NOT_FOUND]} · 本类不适用 {counts[_NA]}"
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
        if it.get("status") in (_ATTENTION, _NOT_FOUND) or it.get("status") not in known
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


def _consistency_texts(decision: dict[str, Any]) -> list[str]:
    """决定一致性中文文案（§2.3，reasons 可并存）。"""
    texts = {
        "claim_drift": "主张身份或内容已变化，当前主张与决定时点不一致",
        "evidence_broken": "决定引用的证据票据已失效或无法定位",
    }
    if decision.get("consistency") != "degraded":
        return []
    return [texts.get(r, r) or str(r) for r in decision.get("consistency_reasons") or []]


def _decisions(doc: Any, row: dict[str, Any]) -> None:
    """四、人工决定与确认（2c §4.2）：有决定或有未编号确认才出现本节。"""
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
    # 受理异议反证状态（§4.1：四态中文直陈）——**不依赖决定存在**（v1.5 钉2：
    # 已受理未采纳的异议也要能看到反证状态）
    accepted = [ob for ob in objections if isinstance(ob, dict) and ob.get("accepted")]
    if not decisions and not unnumbered and not accepted:
        return  # Q4：三者皆无才省略整节
    doc.add_heading("四、人工决定与确认", level=1)
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


def _blind_candidates(doc: Any, row: dict[str, Any]) -> None:
    candidates = row.get("blind_candidates") or []
    if not candidates:
        return
    doc.add_heading("五、模型补盲候选（需人工确认）", level=1)
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


def _policy_quotes(doc: Any, row: dict[str, Any]) -> None:
    doc.add_heading("六、政策摘句", level=1)
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
    scope = row.get("export_scope_note") or (
        "报告只含本次读到并展示的内容；未读部分不写入结论"
    )
    p2 = doc.add_paragraph()
    run2 = p2.add_run(scope)
    run2.italic = True
    doc.add_paragraph(f"报告编号 {row.get('id') or ''} · 由合同审查 Agent 自动生成")
