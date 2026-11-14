"""Local-only rule change drafts. Never modifies or publishes effective rules."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.local_access import require_loopback
from app.services.checklist import list_categories, run_checklist
from app.services.rule_snapshot import capture_rule_snapshot
from app.services.store import store

router = APIRouter(prefix="/rule-drafts")


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(min_length=1, max_length=64)
    operation: Literal["add", "edit", "disable", "enable"]
    item_id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=1000)
    reason: str = Field(min_length=1, max_length=500)
    segment: str = Field(default="", max_length=20)
    literal: str = Field(default="", max_length=120)
    positive: str = Field(default="", max_length=2000)
    negative: str = Field(default="", max_length=2000)
    base_sha256: str = Field(min_length=64, max_length=64)
    revision: int | None = Field(default=None, ge=1)


def _guard(request: Request, *, write: bool = False) -> Path:
    if os.getenv("RULE_DRAFTS_LOCAL_ENABLED") != "1":
        raise HTTPException(404, "本机规则草稿未开启")
    path = os.getenv("RULE_DRAFTS_DB_PATH", "").strip()
    if not path or not Path(path).is_absolute():
        raise HTTPException(503, "请配置独立的本机草稿数据库")
    draft_path = Path(path).resolve()
    review_path = Path(store._path).resolve()
    if draft_path == review_path or (draft_path.exists() and review_path.exists()
                                     and os.path.samefile(draft_path, review_path)):
        raise HTTPException(503, "草稿数据库必须与审查数据库分开")
    require_loopback(request, write=write)
    return draft_path


@contextmanager
def _connection(path: Path):
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        with conn:
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("CREATE TABLE IF NOT EXISTS rule_drafts (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL)")
            yield conn
    finally:
        conn.close()


def _hardline(value: Any) -> bool:
    if isinstance(value, dict):
        return str(value.get("class") or "").strip().lower() == "hardline" or any(
            _hardline(v) for v in value.values()
        )
    return isinstance(value, list) and any(_hardline(v) for v in value)


def _prepare(payload: DraftInput) -> dict[str, Any]:
    if not payload.reason.strip():
        raise HTTPException(422, "请填写变更理由")
    if payload.category not in {c["id"] for c in list_categories()}:
        raise HTTPException(422, "请选择已开放的合同类型")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", payload.item_id):
        raise HTTPException(422, "检查项编号须为小写字母开头，仅含小写字母、数字和下划线")
    snapshot = capture_rule_snapshot(payload.category)
    cfg = snapshot.configuration()
    digest = hashlib.sha256(snapshot.config_bytes).hexdigest()
    if payload.base_sha256 != digest:
        raise HTTPException(409, "规则基线已变化，请重新查看后再试")
    item = next((i for i in cfg["items"] if i.get("id") == payload.item_id), None)
    if payload.operation == "add":
        if item:
            raise HTTPException(409, "检查项编号已存在")
        if not all(v.strip() for v in (payload.name, payload.description, payload.literal, payload.positive, payload.negative)):
            raise HTTPException(422, "新增检查项须填写名称、说明、关键词和正反例")
        if payload.segment not in {str(s.get("key")) for s in (cfg.get("scorecard") or {}).get("segments", [])}:
            raise HTTPException(422, "请选择有效的评分分段")
        before = None
        after = {"name": payload.name, "description": payload.description, "segment": payload.segment,
                 "literal": payload.literal, "enabled": True}
        candidate = {"id": payload.item_id, "name": payload.name, "segment": payload.segment,
                     "description": payload.description, "class": "existence",
                     "rules": {"pass": [{"pattern": re.escape(payload.literal)}]}}
        config = {"category": payload.category, "items": [candidate]}
        positive = run_checklist(payload.positive, payload.category, configuration=config)["items"][0]["status"]
        negative = run_checklist(payload.negative, payload.category, configuration=config)["items"][0]["status"]
        if positive != "通过" or negative != "未找到":
            raise HTTPException(422, "候选规则未通过正反例测试")
        validation = {"passed": True, "scope": "已用规则引擎检查关键词正反例；未验证评分、历史回归或法律适用性"}
    else:
        if item is None:
            raise HTTPException(404, "检查项不存在")
        if payload.operation in {"disable", "edit"} and _hardline(item):
            raise HTTPException(422, "含硬性规则的检查项不能通过普通草稿修改或停用")
        enabled = item.get("enabled", True)
        if payload.operation == "enable" and enabled:
            raise HTTPException(422, "该检查项已经启用")
        if payload.operation == "disable" and not enabled:
            raise HTTPException(422, "该检查项已经停用")
        if payload.operation == "edit" and not (payload.name.strip() and payload.description.strip()):
            raise HTTPException(422, "请填写检查项名称和说明")
        before = {"name": item.get("name"), "description": item.get("description", ""),
                  "enabled": enabled}
        after = dict(before)
        if payload.operation == "edit":
            after.update(name=payload.name, description=payload.description)
        else:
            after["enabled"] = payload.operation == "enable"
        validation = {"passed": False, "scope": "仅保存变更意图；依赖检查、规则测试和历史回归尚未执行"}
    return {**payload.model_dump(exclude={"revision", "base_sha256"}), "base_sha256": digest,
            "before": before, "after": after, "status": "draft", "validation": validation}


@router.get("/capability")
def capability(request: Request):
    try:
        _guard(request)
    except HTTPException:
        return {"available": False, "local_only": True}
    return {"available": True, "local_only": True}


@router.get("/catalog")
def draft_catalog(request: Request):
    _guard(request)
    packs = []
    for category in list_categories():
        snapshot = capture_rule_snapshot(category["id"])
        cfg = snapshot.configuration()
        if cfg.get("hidden"):
            continue
        packs.append({"id": category["id"], "label": cfg.get("label", category["id"]),
                      "base_sha256": hashlib.sha256(snapshot.config_bytes).hexdigest(),
                      "items": [{"id": i["id"], "name": i["name"],
                                 "description": i.get("description", ""),
                                 "has_hardline": _hardline(i),
                                 "enabled": i.get("enabled", True)} for i in cfg["items"]],
                      "segments": [{"key": s.get("key"), "name": s.get("name")} for s in
                                   (cfg.get("scorecard") or {}).get("segments", [])]})
    return {"packs": packs}


@router.get("")
def list_drafts(request: Request):
    path = _guard(request)
    with _connection(path) as conn:
        rows = conn.execute("SELECT data FROM rule_drafts ORDER BY rowid DESC").fetchall()
    return {"drafts": [json.loads(row[0]) for row in rows]}


@router.post("")
def create_draft(payload: DraftInput, request: Request):
    path = _guard(request, write=True)
    data = _prepare(payload)
    data.update(id=uuid.uuid4().hex, revision=1, created_at=time.time(), updated_at=time.time())
    with _connection(path) as conn:
        conn.execute("INSERT INTO rule_drafts (id, revision, data) VALUES (?, ?, ?)",
                     (data["id"], data["revision"], json.dumps(data, ensure_ascii=False)))
    return data


@router.put("/{draft_id}")
def update_draft(draft_id: str, payload: DraftInput, request: Request):
    path = _guard(request, write=True)
    if not re.fullmatch(r"[0-9a-f]{32}", draft_id) or payload.revision is None:
        raise HTTPException(422, "草稿编号或版本无效")
    with _connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT revision, data FROM rule_drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "草稿不存在")
        old = json.loads(row[1])
        if row[0] != payload.revision or old["category"] != payload.category:
            raise HTTPException(409, "草稿已变化，请刷新后重试")
        current = _prepare(payload)
        if current["base_sha256"] != old["base_sha256"]:
            raise HTTPException(409, "规则基线已变化，请重新创建草稿")
        current.update(id=draft_id, revision=row[0] + 1, created_at=old["created_at"], updated_at=time.time())
        conn.execute("UPDATE rule_drafts SET revision=?, data=? WHERE id=?", (current["revision"],
                     json.dumps(current, ensure_ascii=False), draft_id))
    return current


@router.delete("/{draft_id}")
def delete_draft(draft_id: str, revision: int, request: Request):
    path = _guard(request, write=True)
    if not re.fullmatch(r"[0-9a-f]{32}", draft_id) or revision < 1:
        raise HTTPException(422, "草稿编号或版本无效")
    with _connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT revision FROM rule_drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "草稿不存在")
        if row[0] != revision:
            raise HTTPException(409, "草稿已变化，请刷新后重试")
        conn.execute("DELETE FROM rule_drafts WHERE id=?", (draft_id,))
    return {"deleted": True}
