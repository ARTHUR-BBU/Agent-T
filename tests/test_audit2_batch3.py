"""外部审计批 3 整改测试：unless 残余收敛、TTL 落盘清理、replay_11 结构收编、
流式分块真锁、定金对称罚则豁免。

对应「代码质量审查报告｜第一轮」遗留项 + PR#22/23/24 门禁挂账。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.services.checklist import load_checklist, run_checklist, _eval_item
from app.services.store import ReviewStore

ROOT = Path(__file__).resolve().parents[1]


# ---------- ① replay_11 结构收编（真实漏检回归，精简文本规避版权语料） ----------

_FILLER = "双方就维修责任、水电费用、物业服务等事项另行协商约定，条款内容以实际履行情况为准补充。" * 8


def _lease_signature_item() -> dict:
    for item in load_checklist("lease")["items"]:
        if item["id"] == "signature":
            return item
    raise AssertionError("lease 清单必须有 signature 项")


def test_dual_document_only_stamped_second_agreement_fires():
    """replay_11 结构（小智娘批2 实证的真实漏检）：双协议拼接，第一份
    「签字、盖章」俱全并已签署，第二份签署栏**仅盖章**——第二份的
    （盖章）occurrence 距任何「签字」表述远超 60 字，逐 occurrence 判定
    必须独立触发（旧首命中邻域实现会被第一份洗成通过）。"""
    text = (
        "房屋租赁合同（协议一）\n本合同自双方签字、盖章之日起生效。\n"
        "甲方（签字）：张三　乙方（签字）：李四\n"
        + _FILLER +
        "房屋租赁合同（协议二·补充）\n双方确认租赁标的与租金安排如协议一。\n"
        "甲方（盖章）：\n乙方（盖章）：\n"
    )
    out = _eval_item(text, _lease_signature_item())
    assert out["status"] == "需关注", (
        "第二份协议仅盖章必须独立触发（前文签字保护句不得跨文档洗白）"
    )


def test_single_agreement_signed_and_stamped_still_passes():
    """对照：单一协议签字+盖章齐全 → unless 正常放行（不因新语义误伤）。"""
    text = (
        "房屋租赁合同\n本合同自双方签字、盖章之日起生效。\n"
        "甲方（签字）：张三　乙方（签字）：李四\n"
        "甲方（盖章）：某某置业有限公司　乙方（盖章）：某某科技有限公司\n"
    )
    out = _eval_item(text, _lease_signature_item())
    assert out["status"] != "需关注"


# ---------- ② TTL 过期行落盘清理 ----------

def test_expired_row_deleted_on_read(tmp_path):
    """过期行读到即删（小智娘批2 P3-2）：服务长期无新上传时磁盘不滞留。"""
    store = ReviewStore(db_path=str(tmp_path / "t.db"), ttl_seconds=0.05)
    rid = store.create(filename="a.txt", category="lease", status="done")
    time.sleep(0.08)
    assert store.get(rid) is None
    # 直接查库验证已物理删除（而非仅读屏蔽）
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "t.db"))
    count = conn.execute("SELECT COUNT(*) FROM reviews WHERE id = ?", (rid,)).fetchone()[0]
    conn.close()
    assert count == 0, "过期行必须从 SQLite 物理删除"


# ---------- ③ 流式分块行为真锁（肉饼 PR#23 P3-2） ----------

def test_read_limited_stops_reading_midway_on_oversize():
    """超限时读取必须中途停止：read 调用次数有界，剩余字节不进内存。"""
    from app.api.routes import MAX_UPLOAD_BYTES, _READ_CHUNK, _read_limited

    class _FakeFile:
        def __init__(self):
            self.read_calls = 0

        async def read(self, size: int = -1):
            self.read_calls += 1
            return b"x" * size  # 永远有数据（模拟无限大文件）

    fake = _FakeFile()
    data, over = asyncio.run(_read_limited(fake))
    assert over is True
    assert len(data) <= MAX_UPLOAD_BYTES
    expected_calls = MAX_UPLOAD_BYTES // _READ_CHUNK + 1
    assert fake.read_calls == expected_calls, (
        f"超限后必须立即停止读取（期望 ≤{expected_calls} 次，实际 {fake.read_calls} 次）"
    )


def test_read_limited_normal_file_single_pass():
    from app.api.routes import _read_limited

    class _FakeFile:
        def __init__(self, payloads):
            self.payloads = list(payloads)
            self.read_calls = 0

        async def read(self, size: int = -1):
            self.read_calls += 1
            return self.payloads.pop(0) if self.payloads else b""

    fake = _FakeFile(["第一块".encode("utf-8"), "第二块".encode("utf-8")])
    data, over = asyncio.run(_read_limited(fake))
    assert over is False
    assert data == "第一块第二块".encode("utf-8")
    assert fake.read_calls == 3  # 两块数据 + 一次空读终止


# ---------- ④ 定金对称罚则豁免（PR#22 挂账，肉饼下批验收项） ----------

def test_symmetric_deposit_penalty_not_flagged():
    """民法典587 对称定金罚则（不予退还+双倍返还并存）不得直捕需关注。"""
    text = (
        "设备采购合同\n甲方（买方）与乙方（卖方）约定：\n"
        "乙方违约的，已付定金不予退还；甲方违约的，应双倍返还定金。\n"
        "货款验收合格后分期支付。\n争议向甲方所在地法院起诉。适用中华人民共和国法律。\n"
    )
    items = {i["id"]: i["status"] for i in run_checklist(text, "procurement")["items"]}
    assert items["unfair_terms"] != "需关注", (
        "对称定金罚则是法定安排，不得因「不予退还」单字样直捕"
    )


def test_onesided_deposit_forfeit_still_flagged():
    """单方定金没收（pc11 结构，无对等罚则）仍必须命中（金标不回退）。"""
    text = open(ROOT / "fixtures/precheck/pc11_sale_seller_view.txt", encoding="utf-8").read()
    items = {i["id"]: i["status"] for i in run_checklist(text, "procurement")["items"]}
    assert items["unfair_terms"] == "需关注", "pc11 金标：单方定金没收仍需直捕"


def test_onesided_deposit_forward_order_discriminative_lock():
    """正序「定金…不予退还」判别性锁（小智娘批3 P3-2）：pc11 的 unfair_terms
    实靠「逾期视为合格」命中、deposit 正序 pattern 对 pc11 从未命中——
    本用例专锁独立定金子规则，删掉它本测试必须炸。"""
    text = (
        "设备采购合同\n甲方（买方）与乙方（卖方）约定：\n"
        "合同签订后乙方支付定金，若乙方中途解约，定金不予退还。\n"
        "货款验收合格后分期支付。\n争议向甲方所在地法院起诉。适用中华人民共和国法律。\n"
    )
    result = run_checklist(text, "procurement")
    unfair = next(i for i in result["items"] if i["id"] == "unfair_terms")
    assert unfair["status"] == "需关注"
    assert any("定金" in h for h in unfair["hits"]), (
        "必须由独立定金子规则直捕（判别性锁：命中词不含定金即说明锁失效）"
    )
