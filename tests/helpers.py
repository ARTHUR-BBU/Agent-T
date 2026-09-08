"""测试共享工具。"""
from __future__ import annotations

import time


def wait_review_done(client, review_id: str, timeout: float = 30.0) -> dict:
    """审查已改后台任务：轮询直到 done/error（上传接口即时返回 review_id）。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f"/api/review/{review_id}")
        assert r.status_code == 200, f"查询失败 {r.status_code}"
        last = r.json()
        if last.get("status") not in ("processing", "pending"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"审查 {timeout}s 内未完成，最后状态 {last and last.get('status')}")


def upload_and_wait(client, path, category: str, timeout: float = 30.0) -> dict:
    """上传 fixtures 合同并等待审查完成，返回最终结果。"""
    from pathlib import Path
    p = Path(path)
    r = client.post(
        "/api/upload",
        files={"file": (p.name, p.read_bytes(), "text/plain")},
        data={"category": category},
    )
    assert r.status_code == 200, f"上传失败 {r.status_code}: {r.text}"
    rid = r.json()["review_id"]
    return wait_review_done(client, rid, timeout=timeout)
