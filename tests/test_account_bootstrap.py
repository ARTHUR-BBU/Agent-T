"""The first administrator can only be provisioned through the local CLI."""
from app.services import account_bootstrap
from app.services.account_store import AccountStore


def test_bootstrap_cli_creates_once_without_printing_password(tmp_path, monkeypatch, capsys):
    db = tmp_path / "accounts.sqlite"
    password = "test-secret-password-123"
    monkeypatch.setattr(account_bootstrap.getpass, "getpass", lambda _: password)
    args = ["--db", str(db), "--username", "admin", "--name", "管理员"]
    assert account_bootstrap.main(args) == 0
    assert password not in capsys.readouterr().out
    token = AccountStore(db).login("admin", password)
    assert AccountStore(db).authenticate(token)["role"] == "admin"
    assert account_bootstrap.main(args) == 1


def test_bootstrap_rejects_review_database_before_password_prompt(tmp_path, monkeypatch):
    db = tmp_path / "reviews.sqlite"
    monkeypatch.setenv("STORE_DB_PATH", str(db))
    monkeypatch.setattr(account_bootstrap.getpass, "getpass", lambda _: (_ for _ in ()).throw(AssertionError()))
    assert account_bootstrap.main(["--db", str(db), "--username", "a", "--name", "A"]) == 2
    assert not db.exists()
