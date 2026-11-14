"""Trial confirmations are short-lived and consumed once for exact input/version."""
from concurrent.futures import ThreadPoolExecutor

from app.services import rule_trial_confirmation as tickets


def test_confirmation_rejects_changed_input_and_cannot_be_replayed():
    choice = ("a" * 32, 3, "b" * 64, "neutral")
    token = tickets.issue("c" * 64, "pasted_text", {"procurement": choice})
    assert not tickets.consume(token, document_sha256="d" * 64, input_kind="pasted_text",
                               category="procurement", selected_version=choice)
    assert not tickets.consume(token, document_sha256="c" * 64, input_kind="pasted_text",
                               category="procurement", selected_version=choice)


def test_only_one_concurrent_consumer_and_expiration(monkeypatch):
    choice = ("a" * 32, 1, "b" * 64, "neutral")
    token = tickets.issue("c" * 64, "file:.txt", {"procurement": choice})

    def consume(_):
        return tickets.consume(token, document_sha256="c" * 64, input_kind="file:.txt",
                               category="procurement", selected_version=choice)

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(consume, range(8))) == 1
    now = tickets.time.monotonic()
    monkeypatch.setattr(tickets.time, "monotonic", lambda: now)
    expired = tickets.issue("c" * 64, "pasted_text", {"procurement": choice})
    monkeypatch.setattr(tickets.time, "monotonic", lambda: now + 301)
    assert not tickets.consume(expired, document_sha256="c" * 64, input_kind="pasted_text",
                               category="procurement", selected_version=choice)


def test_confirmation_rejects_changed_type_stance_or_version():
    choice = ("a" * 32, 1, "b" * 64, "neutral")
    attempts = [
        ("file:.pdf", "procurement", choice),
        ("file:.txt", "lease", choice),
        ("file:.txt", "procurement", ("a" * 32, 1, "b" * 64, "buyer")),
        ("file:.txt", "procurement", ("a" * 32, 2, "b" * 64, "neutral")),
    ]
    for kind, category, selected in attempts:
        token = tickets.issue("c" * 64, "file:.txt", {"procurement": choice})
        assert not tickets.consume(token, document_sha256="c" * 64, input_kind=kind,
                                   category=category, selected_version=selected)
