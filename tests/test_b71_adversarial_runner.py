# -*- coding: utf-8 -*-
"""B7-1 对抗集实跑器控制流测试（codex 复批 2026-10-25：三组接线问题）。

验证方式：远端 Python 片段对**临时库真实执行**（不 mock 片段本身），
ssh/HTTP 边界用假 transport 模拟（零网络零费用）。覆盖：
① 种单/查账/预算片段真实可执行 + argv/stdin 通道分离；
② --live 不得绕过 dry-run 前提预检；圈账按本批真实单号；费用未知即停；
③ 连续两次基础设施失败（llm_error）即停且不判 PASS；HTTP 超时留痕并走末次结算。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from tools.m7 import adversarial_runner as runner


@pytest.fixture()
def server_db(tmp_path):
    db = tmp_path / "server.db"
    con = sqlite3.connect(db)
    for ddl in (
        "CREATE TABLE reviews (id TEXT PRIMARY KEY, created_at REAL, data TEXT)",
        "CREATE TABLE llm_calls (call_id TEXT PRIMARY KEY, node TEXT,"
        " review_id TEXT, outcome TEXT, error_detail TEXT, raw_output TEXT,"
        " created_at REAL)",
        "CREATE TABLE revision_budget (review_id TEXT PRIMARY KEY,"
        " revision_used INTEGER DEFAULT 0, impact_used INTEGER DEFAULT 0)",
        "CREATE TABLE revision_drafts (id TEXT PRIMARY KEY, review_id TEXT,"
        " item_id TEXT, created_at REAL, trace_id TEXT, original_quote TEXT,"
        " payload TEXT, status TEXT)",
    ):
        con.execute(ddl)
    con.commit()
    con.close()
    return str(db)


def _run_fragment(code: str, db: str, args: tuple = (),
                  stdin: str | None = None):
    """真实执行远端片段（subprocess 直跑，非 mock）。"""
    return subprocess.run([sys.executable, "-X", "utf8", "-c", code, db, *args],
                          input=stdin, capture_output=True, text=True,
                          check=False)


# ---------- ① 片段真实执行（漏 import sqlite3 / argv 通道必炸） ----------

def test_seed_fragment_actually_inserts(server_db):
    r = _run_fragment(runner.SEED_CODE, server_db,
                      stdin=json.dumps([{"id": "b7a1",
                                         "data": {"filename": "x"}}],
                                       ensure_ascii=False))
    assert r.returncode == 0, r.stderr
    assert "seeded 1" in r.stdout
    con = sqlite3.connect(server_db)
    row = con.execute("SELECT data FROM reviews WHERE id='b7a1'").fetchone()
    con.close()
    assert row and "x" in row[0], "种单片段必须真实落库（漏 import sqlite3 即炸）"


def test_ledger_and_budget_fragments_use_argv_channel(server_db):
    """rid 走 argv 通道（codex：曾误当 stdin 发送 → IndexError）。"""
    con = sqlite3.connect(server_db)
    con.execute("INSERT INTO reviews VALUES ('r1', 0, '{}')")
    con.execute("INSERT INTO llm_calls VALUES ('c1', 'revision', 'r1',"
                " 'success', NULL, NULL, 0)")
    con.execute("INSERT INTO revision_budget VALUES ('r1', 2, 0)")
    con.commit()
    con.close()
    r = _run_fragment(runner.LEDGER_ROWS_CODE, server_db, args=("r1", "revision"))
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout.strip().splitlines()[-1])
    assert len(rows) == 1 and rows[0]["call_id"] == "c1"
    r = _run_fragment(runner.BUDGET_USED_CODE, server_db, args=("r1",))
    # v4 契约：额度按节点分列返回 [revision_used, impact_used]
    assert r.returncode == 0
    assert json.loads(r.stdout.strip().splitlines()[-1]) == [2, 0]


def test_ssh_py_routes_args_to_argv(monkeypatch):
    """通道分离断言：args 必须出现在远端命令 argv，stdin 只承载 payload。"""
    captured = {}

    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        captured["cmd"] = cmd
        captured["stdin"] = input
        return subprocess.CompletedProcess(cmd, 0, "seeded 1\n", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner._ssh_py("host", runner.SEED_CODE, "agent-t",
                   stdin_text=json.dumps([{"id": "r9", "data": {}}]))
    cmd_joined = " ".join(captured["cmd"])
    assert "agent-t" in cmd_joined
    assert "r9" not in cmd_joined and "r9" in (captured["stdin"] or "")


# ---------- 模拟网络环境（ssh 边界假、远端片段真执行） ----------

class FakeServer:
    def __init__(self, db: str):
        self.db = db

    def ssh_py(self, server_host, code, db, args=(), stdin_text=None):
        r = _run_fragment(code, self.db, args=args, stdin=stdin_text)
        if r.returncode != 0:
            raise runner.LedgerQueryFailure(
                f"rc={r.returncode}: {r.stderr[-300:]}")
        return r.stdout


def _fake_env(monkeypatch, tmp_path, post=None, spent=(800, 4000)):
    """搭建模拟环境；post=None 时默认拒绝题形态（422）。返回五元组。"""
    server_db = str(tmp_path / "server.db")
    con = sqlite3.connect(server_db)
    for ddl in (
        "CREATE TABLE reviews (id TEXT PRIMARY KEY, created_at REAL, data TEXT)",
        "CREATE TABLE llm_calls (call_id TEXT PRIMARY KEY, node TEXT,"
        " review_id TEXT, outcome TEXT, error_detail TEXT, raw_output TEXT,"
        " created_at REAL)",
        "CREATE TABLE revision_budget (review_id TEXT PRIMARY KEY,"
        " revision_used INTEGER DEFAULT 0, impact_used INTEGER DEFAULT 0)",
        "CREATE TABLE revision_drafts (id TEXT PRIMARY KEY, review_id TEXT,"
        " item_id TEXT, created_at REAL, trace_id TEXT, original_quote TEXT,"
        " payload TEXT, status TEXT)",
    ):
        con.execute(ddl)
    con.commit()
    con.close()
    fake = FakeServer(server_db)
    monkeypatch.setattr(runner, "_ssh_py", fake.ssh_py)
    monkeypatch.setattr(runner, "server_revision", lambda host: "b7fake")
    monkeypatch.setattr(runner, "check_revision",
                        lambda caller, tag: (True, ""))
    monkeypatch.setattr(runner, "deepseek_balance", lambda key: 99.0)
    spent_calls: list[list[str]] = []

    def fake_spent(host, rids):
        # v4 严格圈账契约（五轮按节点分列）：(ins, outs, unknown, per_rid)
        spent_calls.append(list(rids))
        if spent is None:
            raise RuntimeError("ledger down")
        ins, outs = spent
        return ins, outs, 0, {r: {"revision": [ins, outs, 0, 1]}
                              for r in rids}

    monkeypatch.setattr(runner, "_spent_strict", fake_spent)
    posts: list[str] = []

    def default_post(url, auth=None, timeout=None):
        posts.append(url)
        return SimpleNamespace(status_code=422,
                               json=lambda: {"detail": "item_not_revisable"})

    monkeypatch.setattr(runner.httpx, "post", post or default_post)
    auth_file = tmp_path / "auth"
    auth_file.write_text("user=u\npassword=p\ndeepseek_key=dk\n",
                         encoding="utf-8")
    return {"posts": posts, "spent_calls": spent_calls,
            "server_db": server_db, "auth_path": str(auth_file)}


def _manifest() -> dict:
    return json.loads((runner.REPO / "fixtures" / "m7" / "manifest.json")
                      .read_text(encoding="utf-8"))


_OK_BODY = {"status": "ok", "draft": {
    "declaration": "参考，不构成法律意见",
    "versions": [{"stance": "稳妥", "text": "a", "note": "n"},
                 {"stance": "平衡", "text": "b", "note": "n"},
                 {"stance": "进取", "text": "c", "note": "n"}],
    "rationale": "r", "original_quote": "q"}}


# ---------- ② --live 不得绕过前提预检（codex 复批 P1） ----------

def test_live_never_bypasses_dry_preflight(tmp_path, monkeypatch):
    monkeypatch.chdir(runner.REPO)
    called = {"http": 0}

    def fake_post(url, auth=None, timeout=None):
        called["http"] += 1
        raise AssertionError("dry 预检失败后不得发出任何请求")

    monkeypatch.setattr(runner.httpx, "post", fake_post)
    monkeypatch.setattr(runner, "run_dry", lambda m, f: 1)  # 前提预检失败
    rc = runner.run_live(_manifest(), "http://x", "auth", "host",
                         tmp_path / "out.json")
    # 闸门先于 _auth/凭据——dry 失败连 auth 文件都不读（不存在也不报错）
    assert rc == 1 and called["http"] == 0


# ---------- ② 费用未知 → 停止后续请求 + 整批不通过 ----------

def test_unknown_cost_stops_further_requests(tmp_path, monkeypatch):
    monkeypatch.chdir(runner.REPO)
    env = _fake_env(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_account",
                        lambda host, rids, expect_paid=(): None)  # 圈账失败
    rc = runner.run_live(_manifest(), "http://x", env["auth_path"], "host",
                         tmp_path / "out.json")
    assert rc == 1
    assert len(env["posts"]) == 1, "首例圈账失败后不得发出任何后续请求"
    out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    # 停止原因必须如实记录「圈账失败，费用未知」；末次结算若恢复了费用
    # 已知（cost_known=True）不算翻案——本轮仍 FAIL（codex P2-4/P2-6 口径）
    assert "费用未知" in (out["stop_reason"] or "")
    fails = [r for r in out["results"] if r.get("pass") is not True]
    assert fails, "圈账失败例不得判 PASS"


# ---------- ② 圈账按本批真实单号（拒绝例意外调用也计入） ----------

def test_accounting_uses_real_rids(tmp_path, monkeypatch):
    monkeypatch.chdir(runner.REPO)

    def ok_post(url, auth=None, timeout=None):
        # 按单号前缀路由（确定性）：b7advb*=拒绝题 422；其余=live 200
        rid = url.split("/api/review/")[1].split("/")[0]
        if rid.startswith("b7advb"):
            return SimpleNamespace(status_code=422,
                                   json=lambda: {"detail": "item_not_revisable"})
        return SimpleNamespace(status_code=200, json=lambda: dict(_OK_BODY))

    env = _fake_env(monkeypatch, tmp_path, post=ok_post)
    rc = runner.run_live(_manifest(), "http://x", env["auth_path"], "host",
                         tmp_path / "out.json")
    assert rc == 0
    # 最后一次圈账必须包含全部真实单号（10 例=8 基础+V1/V2 委婉变体；含拒绝例）
    assert len(env["spent_calls"][-1]) == 10


# ---------- ③ 连续两次基础设施失败（llm_error）即停且不判 PASS ----------

def test_two_infra_degradations_stop_and_fail(tmp_path, monkeypatch):
    monkeypatch.chdir(runner.REPO)
    calls = {"n": 0}

    def post(url, auth=None, timeout=None):
        calls["n"] += 1
        return SimpleNamespace(status_code=200, json=lambda: {
            "status": "degraded", "reason": "llm_error"})

    env = _fake_env(monkeypatch, tmp_path, post=post)
    rc = runner.run_live(_manifest(), "http://x", env["auth_path"], "host",
                         tmp_path / "out.json")
    assert rc == 1, "连续两次基础设施失败必须整批 FAIL"
    out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert "连续 2 次调用失败" in (out["stop_reason"] or "")
    fails = [r for r in out["results"] if r.get("pass") is False]
    assert len(fails) >= 2, "llm_error 不得算作防线合格"
    assert calls["n"] == 2, "第二次基础设施失败后必须停止后续请求"


def test_mixed_failure_classes_share_one_counter(tmp_path, monkeypatch):
    """两类失败合并同一本连续失败计数册（codex 复批 2026-10-25）：
    transport 失败 + 基础设施失败背靠背 = 连续 2 次 → 即停；
    中间夹一次成功则清零，第三次失败不得误报连续两次。"""
    monkeypatch.chdir(runner.REPO)
    plan = {"i": 0}

    def mixed_post(url, auth=None, timeout=None):
        i = plan["i"]
        plan["i"] += 1
        if i == 0:   # A1：transport 失败
            raise httpx.ReadTimeout("simulated timeout")
        if i == 1:   # A2：基础设施失败 → 连续 2 次，即停
            return SimpleNamespace(status_code=200, json=lambda: {
                "status": "degraded", "reason": "llm_error"})
        return SimpleNamespace(status_code=422,
                               json=lambda: {"detail": "item_not_revisable"})

    env = _fake_env(monkeypatch, tmp_path, post=mixed_post)
    rc = runner.run_live(_manifest(), "http://x", env["auth_path"], "host",
                         tmp_path / "out.json")
    out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert rc == 1
    assert "连续 2 次调用失败" in (out["stop_reason"] or ""), \
        "两类失败必须合并计数（混连即停）"
    assert calls_n(out) == 2, "即停后不得发出第三个请求"


def calls_n(out: dict) -> int:
    """实际发出的 revision 请求数（= live 例 http 有响应的条数）。"""
    return sum(1 for r in out["results"] if r["kind"] == "live"
               and (r.get("status") or r.get("error")))


def test_success_resets_consecutive_counter(tmp_path, monkeypatch):
    """失败→成功→失败：成功清零，第三次失败不误报「连续两次」。"""
    monkeypatch.chdir(runner.REPO)
    plan = {"i": 0}

    def alternate_post(url, auth=None, timeout=None):
        i = plan["i"]
        plan["i"] += 1
        if i in (0, 2):  # A1、C1 失败（中间隔着 A2/A3 成功与拒绝例）
            raise httpx.ReadTimeout("simulated timeout")
        if i == 1:
            return SimpleNamespace(status_code=200, json=lambda: dict(_OK_BODY))
        return SimpleNamespace(status_code=422,
                               json=lambda: {"detail": "item_not_revisable"})

    env = _fake_env(monkeypatch, tmp_path, post=alternate_post)
    rc = runner.run_live(_manifest(), "http://x", env["auth_path"], "host",
                         tmp_path / "out.json")
    out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    # 全部样例跑完（无中途停止）——失败被成功清零，不误触连续两次
    assert out["stop_reason"] is None, \
        f"成功清零后不得误报连续失败：{out['stop_reason']}"
    assert rc == 1, "存在未过例（两次 timeout）本轮仍不通过"


# ---------- ③ HTTP 超时：留痕 + 末次结算必执行 ----------

def test_http_timeout_records_error_and_settles(tmp_path, monkeypatch):
    monkeypatch.chdir(runner.REPO)

    def fake_post(url, auth=None, timeout=None):
        rid = url.split("/api/review/")[1].split("/")[0]
        if rid.startswith("b7adva1"):  # 仅 A1 超时（前缀路由，随机 hex 不误伤）
            raise httpx.ReadTimeout("simulated timeout")
        return SimpleNamespace(status_code=422,
                               json=lambda: {"detail": "item_not_revisable"})

    env = _fake_env(monkeypatch, tmp_path, post=fake_post)
    rc = runner.run_live(_manifest(), "http://x", env["auth_path"], "host",
                         tmp_path / "out.json")
    assert rc == 1
    out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    errs = [r for r in out["results"] if r.get("error")]
    assert errs and "simulated timeout" in errs[0]["error"], \
        "超时必须留痕（不得静默跳过）"
    assert out["cost_known"] is True, "末次结算必须执行（正常路径费用已知）"
    assert env["spent_calls"], "末次结算必须真的查过账"


# ---------- ② 全过路径：exit 0 + 费用口径=峰值估算 ----------

def test_all_pass_exit_zero_and_estimate_label(tmp_path, monkeypatch):
    monkeypatch.chdir(runner.REPO)
    env = _fake_env(monkeypatch, tmp_path)

    def ok_post(url, auth=None, timeout=None):
        # 按单号前缀路由（确定性）：b7advb*=拒绝题 422；其余=live 200
        rid = url.split("/api/review/")[1].split("/")[0]
        if rid.startswith("b7advb"):
            return SimpleNamespace(status_code=422,
                                   json=lambda: {"detail": "item_not_revisable"})
        return SimpleNamespace(status_code=200, json=lambda: dict(_OK_BODY))

    monkeypatch.setattr(runner.httpx, "post", ok_post)
    rc = runner.run_live(_manifest(), "http://x", env["auth_path"], "host",
                         tmp_path / "out.json")
    assert rc == 0
    out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert all(r.get("pass") for r in out["results"])
    assert "估算" in out["cost_basis"]
