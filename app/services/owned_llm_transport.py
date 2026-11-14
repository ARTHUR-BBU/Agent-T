"""Opt-in account-bound transport; not a complete account worker or access mode."""
from __future__ import annotations

import contextvars
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from app.services.owned_llm_ledger import OwnedLLMLedger


class OwnedTransportError(RuntimeError):
    """Account-mode transport cannot safely continue."""


@dataclass(frozen=True, repr=False)
class _JobContext:
    ledger: OwnedLLMLedger
    token: str = field(repr=False)
    job_id: str = field(repr=False)
    secret: str = field(repr=False)


_context: contextvars.ContextVar[_JobContext | None] = contextvars.ContextVar("owned_llm_job", default=None)
_PROCESS_MODE = os.getenv("OWNED_LLM_EXECUTION", "0")
_TRUSTED_CF_ACCOUNT = os.getenv("CF_ACCOUNT_ID", "")


def enabled() -> bool:
    mode = _PROCESS_MODE
    if mode not in ("", "0", "1"):
        raise OwnedTransportError("账户模型执行配置无效")
    return mode == "1"


@contextmanager
def job_context(ledger: OwnedLLMLedger, token: str, job_id: str, secret: str):
    if not enabled() or not isinstance(ledger, OwnedLLMLedger):
        raise OwnedTransportError("账户模型执行上下文无效")
    marker = _context.set(_JobContext(ledger, token, job_id, secret))
    try:
        yield
    finally:
        _context.reset(marker)


def _provider(url: str, model: str, *, decision: bool) -> str:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
            raise ValueError
        host, path = parsed.hostname, parsed.path
        if decision:
            if (host == "api.cloudflare.com" and model == "clef-flash"
                    and re.fullmatch(r"[a-f0-9]{32}", _TRUSTED_CF_ACCOUNT)
                    and path == f"/client/v4/accounts/{_TRUSTED_CF_ACCOUNT}/ai/run/@cf/cloudflare/clef-flash"):
                return "cloudflare"
        elif host == "api.deepseek.com" and path in {"/chat/completions", "/v1/chat/completions"} and model == "deepseek-v4-flash":
            return "deepseek"
        elif host == "open.bigmodel.cn" and path in {"/api/paas/v4/chat/completions", "/api/coding/paas/v4/chat/completions"} and model == "glm-5.2":
            return "zhipu"
        elif host == "api.x.ai" and path == "/v1/chat/completions" and model == "grok-2-latest":
            return "xai"
    except ValueError:
        pass
    raise OwnedTransportError("账户模型请求目标或模型不受支持")


def execute(*, url: str, model: str, api_key: str, payload: dict, timeout: float,
            node: str | None, review_id: str | None, decision: bool, fallback_reasoning: bool = False):
    ctx = _context.get()
    if ctx is None or not node or not review_id:
        raise OwnedTransportError("账户模型任务缺少授权上下文")
    if not isinstance(payload, dict) or payload.get("model") != model:
        raise OwnedTransportError("账户模型请求内容与授权模型不匹配")
    provider = _provider(url, model, decision=decision)
    try:
        if ctx.ledger.jobs.authorize_outbound(ctx.token, ctx.job_id, ctx.secret) != review_id:
            raise OwnedTransportError("账户模型任务不匹配")
        call_id = ctx.ledger.begin(ctx.token, ctx.job_id, ctx.secret, node=node, provider=provider, model=model)
    except OwnedTransportError:
        raise
    except Exception as exc:
        raise OwnedTransportError("账户模型任务授权或记账失败") from exc
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            response = client.post(url, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=payload)
            if response.status_code >= 300:
                raise OwnedTransportError("账户模型请求失败")
            data = response.json()
            if not isinstance(data, dict):
                raise OwnedTransportError("账户模型返回格式无效")
            if decision:
                value = decision_text(data)
            else:
                choices = data.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict) or not isinstance(choices[0].get("message"), dict):
                    raise OwnedTransportError("账户模型返回格式无效")
                message = choices[0]["message"]
                value = message.get("content")
                if value is None or value == "":
                    value = message.get("reasoning_content") if fallback_reasoning else ""
                if value is None:
                    value = ""
                if not isinstance(value, str):
                    raise OwnedTransportError("账户模型返回格式无效")
                value = value.strip()
                if not value:
                    raise OwnedTransportError("账户模型返回为空")
        usage = data.get("usage") or (data.get("result") or {}).get("usage") or {}
        if not isinstance(usage, dict):
            raise OwnedTransportError("账户模型返回格式无效")
        input_tokens = usage.get("input_tokens" if decision else "prompt_tokens")
        output_tokens = usage.get("output_tokens" if decision else "completion_tokens")
        ctx.ledger.settle(ctx.token, ctx.job_id, ctx.secret, call_id, outcome="success",
                          input_tokens=input_tokens, output_tokens=output_tokens)
        return call_id, value
    except Exception as exc:
        try:
            ctx.ledger.settle(ctx.token, ctx.job_id, ctx.secret, call_id,
                              outcome="timeout" if isinstance(exc, httpx.TimeoutException) else "provider_error")
        except Exception:
            pass  # A revoked/expired job leaves pending; reconcile reports unknown.
        raise OwnedTransportError("账户模型请求未完成") from exc


def decision_text(data: dict) -> str:
    result = data.get("result") or {}
    if not isinstance(result, dict):
        raise OwnedTransportError("账户模型返回格式无效")
    answers = data.get("answers") or result.get("answers")
    if (not isinstance(answers, dict) or "category" not in answers or not isinstance(answers["category"], dict)
            or not answers["category"]):
        raise OwnedTransportError("账户模型返回格式无效")
    return json.dumps(answers.get("category", {}), ensure_ascii=False)


def settle_decision(call_id: str, outcome: str, raw_output: str | None) -> bool:
    ctx = _context.get()
    if ctx is None or outcome not in ("success", "parse_failed") or (outcome == "success" and raw_output is None):
        raise OwnedTransportError("账户模型业务结算无效")
    try:
        ctx.ledger.settle_business(ctx.token, ctx.job_id, ctx.secret, call_id, outcome=outcome)
    except Exception as exc:
        raise OwnedTransportError("账户模型业务结算失败") from exc
    return True
