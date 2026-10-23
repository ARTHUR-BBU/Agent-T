# -*- coding: utf-8 -*-
"""节点输出上限测试（M6.5 收官回测费用闸：真水龙头，Codex P1 2026-10-22）。

生产账本实测输出分布（precheck max 1326 / model_review 6150 / objection 4654 /
quality 12156）× 约 2~2.5 倍余量 = 各节点 max_tokens；未列节点走 DEFAULT。
"""
from __future__ import annotations

from unittest import mock

from app.services import llm_call_log, llm_client


def _capture_payload(node: str) -> dict:
    """直跑 chat_completion，捕获发往供应商的 payload。"""
    captured: dict = {}
    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": "ok"}}],
                              "usage": {}}
    with mock.patch.object(llm_client.httpx, "Client") as fake:
        fake.return_value.__enter__ = lambda s: s
        fake.return_value.__exit__ = lambda s, *a: False

        def post(url, headers=None, json=None, **kw):
            captured.update(json or {})
            return resp

        fake.return_value.post = post
        with llm_call_log.record_node(node, prompt_version="pv"):
            llm_client.chat_completion(
                api_key="k", url="https://api.deepseek.com/v1",
                model="m", system="s", user="u", temperature=0, timeout=5)
    return captured


def test_precheck_cap_applied() -> None:
    assert _capture_payload("precheck")["max_tokens"] == 3000


def test_quality_cap_applied() -> None:
    assert _capture_payload("quality")["max_tokens"] == 25000


def test_unknown_node_uses_default_cap() -> None:
    assert _capture_payload("never_seen_node")["max_tokens"] == 16000


def test_caps_table_matches_ruling_evidence() -> None:
    """上限表与账本实测×余量口径一致（防悄悄调大——改动须走审计）。"""
    assert llm_client.NODE_MAX_OUTPUT_TOKENS == {
        "precheck": 3000, "model_review": 13000, "quality": 25000,
        "objection": 10000, "ask": 8000,
    }
    assert llm_client.DEFAULT_MAX_OUTPUT_TOKENS == 16000


# ---- 畸形 200 响应必须落账（GLM 审计+codex 裁定 2026-10-23）----
# 「请求可能已计费、账本却 0 行」是费用哨兵与证据链的盲区：
# 200 + 坏 JSON / 200 + 缺 choices / 结构错 → provider_error 账一行 + 受控上抛。

def _run_malformed(resp_json) -> tuple[list, BaseException]:
    """直跑 chat_completion，捕获账本 emit 行与抛出的异常。"""
    emitted: list = []
    resp = mock.Mock()
    resp.status_code = 200
    resp.json = resp_json
    with mock.patch.object(llm_client.httpx, "Client") as fake:
        fake.return_value.__enter__ = lambda s: s
        fake.return_value.__exit__ = lambda s, *a: False
        fake.return_value.post = lambda url, headers=None, json=None, **kw: resp
        with mock.patch.object(llm_client.llm_call_log, "emit",
                               side_effect=emitted.append):
            with llm_call_log.record_node("precheck", prompt_version="pv"):
                try:
                    llm_client.chat_completion(
                        api_key="k", url="https://api.deepseek.com/v1",
                        model="m", system="s", user="u", temperature=0, timeout=5)
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    err: BaseException = exc
                else:
                    err = AssertionError("畸形响应必须抛异常，不能静默返回")
    return emitted, err


def test_malformed_json_lands_provider_error_row() -> None:
    """200 + 坏 JSON：provider_error 账一行（不双记）+ 受控上抛。"""
    def bad_json(self=None):
        raise ValueError("Expecting value")
    emitted, err = _run_malformed(bad_json)
    assert isinstance(err, ValueError) and "invalid_json_response" in str(err)
    assert len(emitted) == 1
    assert emitted[0].outcome == "provider_error"
    assert "invalid_json_response" in (emitted[0].error_detail or "")


def test_malformed_choices_lands_provider_error_row() -> None:
    """200 + 缺/空/错结构 choices：provider_error 账一行（不双记）+ 受控上抛。"""
    for payload in ({"usage": {}},                    # 缺 choices
                    {"choices": []},                  # 空数组
                    {"choices": [{"message": "x"}]},  # message 非 dict
                    "not-a-dict"):                    # 顶层都不是对象
        emitted, err = _run_malformed(lambda p=payload: p)
        assert isinstance(err, ValueError) and "malformed_success_response" in str(err)
        assert len(emitted) == 1, f"{payload!r} 落账行数异常"
        assert emitted[0].outcome == "provider_error"
        assert "malformed_success_response" in (emitted[0].error_detail or "")
