# -*- coding: utf-8 -*-
"""B7-3 联合验收样例回放：从 B7-2 付费实跑成绩单（adversarial-run-b72.json）
离线重建草案/选定/牵连数据，导出带「AI 修改建议（AI 起草辅助）」章节的
验收样例报告。

纪律：
- **零 LLM 调用、零付费**——数据来自已留痕的考试产出，纯读库组装
- 复用生产组装路径（impact.revision_report_section + report.build_report_docx），
  不另写演示代码——老钱审到的就是生产逻辑的产物
- 样例覆盖：I1（选定+牵连 ok，双 findings 双摘录）、I2（选定+牵连
  uncovered「不代表没有冲突」）

用法：python -X utf8 tools/m7/replay_report_b73.py
输出：docs/m7/acceptance-report-b73.docx / .txt
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from docx import Document  # noqa: E402

from app.services import impact  # noqa: E402
from app.services import revision  # noqa: E402
from app.services.report import build_report_docx  # noqa: E402
from app.services.store import ReviewStore  # noqa: E402

RUN_JSON = REPO / "docs" / "m7" / "adversarial-run-b72.json"
FIXTURES = REPO / "fixtures" / "m7"
OUT_DOCX = REPO / "docs" / "m7" / "acceptance-report-b73.docx"
OUT_TXT = REPO / "docs" / "m7" / "acceptance-report-b73.txt"

# 参与回放的考题：I1（牵连覆盖）/ I2（牵连未覆盖）——V1/V2 是降级态考题
# （禁语拦截整份作废），报告章节对降级草案如实「无选定」，同样有验收价值
REPLAY_IDS = ("I1", "I2")


def main() -> int:
    run = json.loads(RUN_JSON.read_text(encoding="utf-8"))
    results = {r["id"]: r for r in run["results"]}

    tmp = tempfile.mkdtemp(prefix="b73-acceptance-")
    os.environ["STORE_DB_PATH"] = str(pathlib.Path(tmp) / "acceptance.db")
    store = ReviewStore(db_path=os.environ["STORE_DB_PATH"])

    sections: list[dict] = []
    view_items: list[dict] = []
    for qid in REPLAY_IDS:
        r = results[qid]
        text = (FIXTURES / f"{qid.lower()}-{ 'impact-linked' if qid == 'I1' else 'impact-lonely' }.txt").read_text(
            encoding="utf-8")
        draft = r["draft"]
        quote = draft["original_quote"].replace("…", "").strip() or text[:20]
        item = {"id": f"item_{qid.lower()}", "name": "定金条款公平性",
                "status": "需关注", "note": draft.get("item_note") or "",
                "quote": quote, "hits": [],
                "evidence_start": draft["evidence"]["evidence_start"],
                "evidence_end": draft["evidence"]["evidence_end"],
                "primary_clause_id": draft["evidence"].get("primary_clause_id"),
                "clause_ids": draft["evidence"].get("clause_ids") or []}
        rid = store.create(filename=f"{qid.lower()}-考卷.docx",
                           category="procurement")
        store.update(rid, status="done", items=[item], text=text)

        # 回放草案（考试留痕 payload 原样入库）+ 选定 + 牵连报告
        import sqlite3
        revision._connect().close()  # 触发 revision 两表建表
        with impact._connect() as conn:
            conn.execute(impact._SELECTED_DDL)
            conn.execute(impact._IMPACT_REPORTS_DDL)
        con = sqlite3.connect(os.environ["STORE_DB_PATH"])
        con.execute(
            "INSERT INTO revision_drafts (id, review_id, item_id, created_at,"
            " trace_id, original_quote, payload, status) VALUES (?,?,?,?,?,?,?,?)",
            (r["draft_id"], rid, item["id"], 1.0, "replay-b73",
             quote, json.dumps(draft, ensure_ascii=False), "ok"))
        con.execute(
            "INSERT INTO selected_drafts (review_id, item_id, draft_id,"
            " stance, selected_at) VALUES (?,?,?,?,?)",
            (rid, item["id"], r["draft_id"], r.get("select_stance") or "",
             2.0))
        ir = r.get("impact_report")
        if ir is not None:
            con.execute(
                "INSERT INTO impact_reports (id, review_id, item_id,"
                " draft_id, created_at, trace_id, payload, status, reason,"
                " stance) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"ir-{qid}", rid, item["id"], r["draft_id"], 3.0,
                 "replay-b73", json.dumps(ir, ensure_ascii=False),
                 r["impact_status"], "", r.get("select_stance") or ""))
        con.commit()
        con.close()

        sec = impact.revision_report_section(store, rid)
        assert sec, f"{qid} 组装为空（回放失败）"
        sections.extend(sec)
        view_items.append(item)

    view = {"id": "b73-acceptance", "filename": "B7-3验收样例.docx",
            "category": "procurement", "category_label": "采购合同",
            "status": "done", "created_at": "2026-11-03 10:00",
            "items": view_items,
            "revision_section": sections}
    data = build_report_docx(view)
    OUT_DOCX.write_bytes(data)

    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row_ in table.rows:
            for cell in row_.cells:
                parts.append(cell.text)
    OUT_TXT.write_text("\n".join(parts), encoding="utf-8")

    print(f"OK docx={OUT_DOCX} ({len(data)} bytes)")
    print(f"OK txt={OUT_TXT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
