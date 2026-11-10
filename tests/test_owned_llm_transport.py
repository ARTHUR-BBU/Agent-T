"""Account transport uses a fake HTTP client and an isolated account database."""
import pytest

from app.services import llm_call_log, llm_client, owned_llm_transport
from app.services.owned_llm_ledger import OwnedLLMLedger
from tests.test_owned_review_jobs import jobs  # noqa: F401


def test_account_transport_binds_real_call_path_and_isolates_ledger(jobs, monkeypatch, tmp_path):  # noqa: F811
    accounts, _, service, _, _, alice, _ = jobs
    ledger = OwnedLLMLedger(service)
    rid, job, secret = service.create(alice, filename="虚构合同", category="procurement")
    shared = tmp_path / "shared.sqlite"
    monkeypatch.setenv("STORE_DB_PATH", str(shared))
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "结果"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers, json):
            calls.append((url, json["model"]))
            return FakeResponse()

    monkeypatch.setattr(owned_llm_transport.httpx, "Client", FakeClient)
    args = dict(api_key="fake", url="https://api.deepseek.com/chat/completions", model="deepseek-v4-flash",
                system="系统", user="虚构合同", temperature=0, timeout=2)
    with llm_call_log.record_node("model_review", rid):
        with pytest.raises(owned_llm_transport.OwnedTransportError):
            llm_client.chat_completion(**args)
        with owned_llm_transport.job_context(ledger, alice, job, secret):
            assert llm_client.chat_completion(**args) == "结果"
            with pytest.raises(owned_llm_transport.OwnedTransportError):
                llm_client.chat_completion(**{**args, "url": "https://api.deepseek.com.evil.example/chat/completions"})
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as pool:
                with pytest.raises(owned_llm_transport.OwnedTransportError):
                    pool.submit(llm_client.chat_completion, **args).result()
    assert calls == [(args["url"], args["model"])]
    with accounts._connect() as conn:
        row = conn.execute("SELECT node,provider,model,outcome,input_tokens FROM owned_llm_attempts").fetchone()
        assert tuple(row) == ("model_review", "deepseek", "deepseek-v4-flash", "success", 5)
    llm_call_log.emit(llm_call_log.LLMCallRecord(call_id="test", node="ask", provider="deepseek", model="test"))
    with pytest.raises(owned_llm_transport.OwnedTransportError):
        llm_call_log.settle("test", "success", raw_output="test")
    assert not shared.exists()


def test_account_decision_transport_and_revocation_after_send(jobs, monkeypatch):  # noqa: F811
    accounts, _, service, admin, user, alice, _ = jobs
    ledger = OwnedLLMLedger(service)
    rid, job, secret = service.create(alice, filename="虚构合同", category="procurement")
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    monkeypatch.setattr(owned_llm_transport, "_TRUSTED_CF_ACCOUNT", "a" * 32)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"result": {"answers": {"category": {"value": "采购"}}, "usage": {"input_tokens": 2, "output_tokens": 1}}}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            accounts.set_active(admin, user["id"], False)
            return FakeResponse()

    monkeypatch.setattr(owned_llm_transport.httpx, "Client", FakeClient)
    url = "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/ai/run/@cf/cloudflare/clef-flash"
    with llm_call_log.record_node("category_suggest", rid), owned_llm_transport.job_context(ledger, alice, job, secret):
        with pytest.raises(owned_llm_transport.OwnedTransportError):
            llm_client.decision_judgment(api_key="fake", url=url, model="clef-flash", state="虚构", instructions="分类", criteria={}, timeout=2)
    assert ledger.reconcile() == 1
    with accounts._connect() as conn:
        assert conn.execute("SELECT outcome FROM owned_llm_attempts").fetchone()["outcome"] == "unknown"


def test_bad_protocol_is_failure_and_default_zhipu_allowed(jobs, monkeypatch):  # noqa: F811
    accounts, _, service, _, _, alice, _ = jobs
    ledger = OwnedLLMLedger(service)
    rid, job, secret = service.create(alice, filename="测试", category="procurement")
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    seen = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": "invalid"}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers, json):
            seen.append(url)
            return FakeResponse()

    monkeypatch.setattr(owned_llm_transport.httpx, "Client", FakeClient)
    url = "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"
    with llm_call_log.record_node("quality", rid), owned_llm_transport.job_context(ledger, alice, job, secret):
        with pytest.raises(owned_llm_transport.OwnedTransportError):
            llm_client.chat_completion(api_key="fake", url=url, model="glm-5.2", system="s", user="u", temperature=0, timeout=2)
    assert seen == [url]
    with accounts._connect() as conn:
        assert conn.execute("SELECT outcome FROM owned_llm_attempts").fetchone()["outcome"] == "provider_error"


def test_account_mode_rejects_custom_model_senders(monkeypatch):
    from app.services import model_review, objection, precheck, quality

    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    dummy = lambda *args: "unsafe"  # noqa: E731
    for run in (lambda: model_review.run_model_review(text="测试", items=[], chat_fn=dummy),
                lambda: precheck.run_precheck("测试", "procurement", chat_fn=dummy),
                lambda: quality.run_quality(text="测试", items=[], chat_fn=dummy),
                lambda: objection.run_objections(text="测试", items=[], chat_fn=dummy)):
        with pytest.raises(owned_llm_transport.OwnedTransportError):
            run()


def test_account_transport_error_propagates_through_model_and_pipeline(monkeypatch):
    from app.graph import pipeline
    from app.services import model_review

    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    monkeypatch.setattr(model_review.scorecard, "has_rule_results", lambda items: True)
    monkeypatch.setattr(model_review.llm_ask, "_deepseek_key", lambda: "fake")

    def denied(*args, **kwargs):
        raise owned_llm_transport.OwnedTransportError("denied")

    monkeypatch.setattr(model_review, "_call_llm", denied)
    with pytest.raises(owned_llm_transport.OwnedTransportError):
        model_review.run_model_review(text="测试", items=[{"id": "one"}])
    monkeypatch.setattr(pipeline, "run_model_review", denied)
    with pytest.raises(owned_llm_transport.OwnedTransportError):
        pipeline.node_model_review({"text": "测试", "items": [{"id": "one"}], "review_id": "test"})


def test_account_mode_is_fixed_for_process_lifetime(tmp_path):
    import os
    import subprocess
    import sys

    env = os.environ.copy()
    env["OWNED_LLM_EXECUTION"] = "1"
    env["STORE_DB_PATH"] = str(tmp_path / "reviews.sqlite")
    env["ACCOUNT_DB_PATH"] = str(tmp_path / "accounts.sqlite")
    script = ("import os; from app.services import owned_llm_transport as t; "
              "os.environ['OWNED_LLM_EXECUTION']='0'; print(t.enabled())")
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "True"


def test_account_decision_success_and_invalid_result_are_recorded(jobs, monkeypatch):  # noqa: F811
    import json

    accounts, _, service, _, _, alice, _ = jobs
    ledger = OwnedLLMLedger(service)
    rid, job, secret = service.create(alice, filename="测试", category="procurement")
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    monkeypatch.setattr(owned_llm_transport, "_TRUSTED_CF_ACCOUNT", "a" * 32)
    responses = [{"result": {"answers": {"category": {"value": "采购"}}}}, {"answers": {"category": {}}}]

    class FakeResponse:
        status_code = 200

        def json(self):
            return responses.pop(0)

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(owned_llm_transport.httpx, "Client", FakeClient)
    url = "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/ai/run/@cf/cloudflare/clef-flash"
    args = dict(api_key="fake", url=url, model="clef-flash", state="虚构", instructions="分类", criteria={}, timeout=2)
    with llm_call_log.record_node("category_suggest", rid), owned_llm_transport.job_context(ledger, alice, job, secret):
        with pytest.raises(owned_llm_transport.OwnedTransportError):
            llm_client.decision_judgment(**{**args, "url": url.replace("a" * 32, "b" * 32)})
        with pytest.raises(owned_llm_transport.OwnedTransportError):
            owned_llm_transport.execute(url=url, model="clef-flash", api_key="fake", payload={"model": "other"},
                                        timeout=2, node="category_suggest", review_id=rid, decision=True)
        call_id, result = llm_client.decision_judgment(**args)
        assert json.loads(result) == {"value": "采购"}
        assert llm_call_log.settle(call_id, "parse_failed")
        with pytest.raises(owned_llm_transport.OwnedTransportError):
            llm_client.decision_judgment(**args)
    with accounts._connect() as conn:
        row = conn.execute("SELECT outcome,business_outcome FROM owned_llm_attempts WHERE id=?", (call_id,)).fetchone()
        assert tuple(row) == ("success", "parse_failed")
        assert sorted(row["outcome"] for row in conn.execute("SELECT outcome FROM owned_llm_attempts")) == ["provider_error", "success"]


def test_real_category_parser_records_business_failure(jobs, monkeypatch, tmp_path):  # noqa: F811
    from app.services import category_suggest

    accounts, _, service, _, _, alice, _ = jobs
    ledger = OwnedLLMLedger(service)
    rid, job, secret = service.create(alice, filename="测试", category="procurement")
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    monkeypatch.setattr(owned_llm_transport, "_TRUSTED_CF_ACCOUNT", "a" * 32)
    monkeypatch.setattr(category_suggest, "is_enabled", lambda: True)
    monkeypatch.setattr(category_suggest, "_load_env", lambda: {"CF_API_TOKEN": "fake", "CF_ACCOUNT_ID": "a" * 32})
    shared = tmp_path / "shared.sqlite"
    monkeypatch.setenv("STORE_DB_PATH", str(shared))

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"result": {"answers": {"category": {"choice": "unsupported"}}}}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(owned_llm_transport.httpx, "Client", FakeClient)
    with llm_call_log.record_node("category_suggest", rid), owned_llm_transport.job_context(ledger, alice, job, secret):
        outcome = category_suggest.suggest_category("虚构合同", "procurement")
    assert outcome.skip_reason == "parse_failed"
    with accounts._connect() as conn:
        row = conn.execute("SELECT outcome,business_outcome FROM owned_llm_attempts").fetchone()
        assert tuple(row) == ("success", "parse_failed")
    assert not shared.exists()
