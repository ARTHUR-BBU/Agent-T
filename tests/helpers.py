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


def upload_and_wait(
    client,
    path,
    category: str,
    timeout: float = 30.0,
    review_id: str | None = None,
) -> dict:
    """上传 fixtures 合同并等待审查完成，返回最终结果。

    review_id 传入时跳过上传（用于 category_confirm 后带最终品类重传的用例）。
    """
    from pathlib import Path

    if review_id is None:
        p = Path(path)
        r = client.post(
            "/api/upload",
            files={"file": (p.name, p.read_bytes(), "text/plain")},
            data={"category": category},
        )
        assert r.status_code == 200, f"上传失败 {r.status_code}: {r.text}"
        body = r.json()
        # 预审拦截守卫（小智娘回归风险清单 #1）：被 category_confirm 拦下时
        # 显式报「被预审拦截」，不能让 KeyError 爆得莫名其妙
        assert body.get("status") != "category_confirm", (
            "上传被预审拦截（category_confirm），"
            f"detected={body.get('precheck') and body['precheck'].get('detected_type')}，"
            f"suggested={body.get('suggested_category')}"
        )
        rid = body["review_id"]
    else:
        rid = review_id
    return wait_review_done(client, rid, timeout=timeout)
