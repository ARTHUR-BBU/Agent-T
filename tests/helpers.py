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


def drain_review_slots(timeout: float = 15.0) -> None:
    """等并发审查槽位全部空闲（CI 稳定性 P2 根治，审计定性强项非重跑关闭）。

    根因：_review_slots 是进程级 Semaphore(MAX_CONCURRENT_REVIEWS)，而多数
    上传测试不等 done 就返回——弱机 CI 上前序测试的后台 worker 还占着槽，
    下一个 upload 即 429「审查排队已满」。此前的限频桶清理只修了频率 429，
    槽位 429 是另一根因（run 35443962586 复发实证）。

    做法与 _rate_limit_reset 同构：autouse 夹具在每个测试 setup 时尝试一次
    性占满全部槽——占到即证明上一测试的 worker 已全部归还；占不到则短轮询
    等待，超时按「后台审查线程疑似泄漏」显式报错（宁可红得可诊断，不要随机
    429）。仅供测试基建使用，生产代码不得调用。
    """
    from app.api import routes

    total = routes.MAX_CONCURRENT_REVIEWS
    deadline = time.time() + timeout
    while True:
        acquired = 0
        while acquired < total and routes._review_slots.acquire(blocking=False):
            acquired += 1
        if acquired == total:
            routes._review_slots.release(total)
            return
        if acquired:
            routes._review_slots.release(acquired)
        if time.time() >= deadline:
            raise AssertionError(
                f"并发审查槽位 {timeout}s 未排空（{total} 个中仍有占用）——"
                "后台审查线程疑似泄漏或过慢，请排查而非调大超时"
            )
        time.sleep(0.05)
