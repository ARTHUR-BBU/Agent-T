"""Basic Auth 中间件测试（部署公开 URL 的访问控制）。

凭据环境变量逐请求读取，monkeypatch 即可动态开关；
不配 / 只配一半 → 认证关闭；配齐 → 除 /health 全站 401。
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _creds(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


_AUTH_ENV = {"BASIC_AUTH_USERNAME": "partner", "BASIC_AUTH_PASSWORD": "s3cret-合同"}


def test_auth_disabled_by_default(monkeypatch):
    for k in _AUTH_ENV:
        monkeypatch.delenv(k, raising=False)
    assert client.get("/").status_code == 200
    assert client.get("/api/categories").status_code == 200


def test_auth_half_config_refuses_to_start(monkeypatch):
    """半配置是部署事故（如 HF Secret 名打错）：必须启动即拒绝（fail-closed），
    不能静默关闭认证让公网裸奔（肉饼审计 P1-1）."""
    from app.auth import validate_auth_config

    monkeypatch.setenv("BASIC_AUTH_USERNAME", "partner")
    monkeypatch.delenv("BASIC_AUTH_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="BASIC_AUTH"):
        validate_auth_config()
    # 反向同理
    monkeypatch.delenv("BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "x")
    with pytest.raises(RuntimeError):
        validate_auth_config()


# ---------- 绕过尝试类（肉饼审计 P2-4：路径变体不得借 /health 豁免打到业务路由） ----------

def test_auth_path_bypass_variants_blocked(monkeypatch):
    monkeypatch.setenv("BASIC_AUTH_USERNAME", _AUTH_ENV["BASIC_AUTH_USERNAME"])
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", _AUTH_ENV["BASIC_AUTH_PASSWORD"])
    # 双斜杠 / 尾斜杠 / 大写 / 前缀相似路径 → 都不是精确 /health，必须 401
    for path in ("//health", "/health/", "/HEALTH", "/healthx", "/Health"):
        r = client.get(path)
        assert r.status_code == 401, f"{path} 不应借道豁免路径"
    # 精确 /health 且带查询串 → 仍豁免（路径匹配不受 query 影响）
    assert client.get("/health?x=1").status_code == 200


def test_auth_non_utf8_credentials_401(monkeypatch):
    monkeypatch.setenv("BASIC_AUTH_USERNAME", _AUTH_ENV["BASIC_AUTH_USERNAME"])
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", _AUTH_ENV["BASIC_AUTH_PASSWORD"])
    token = base64.b64encode(b"\xff\xfe:abc").decode("ascii")
    r = client.get("/", headers={"Authorization": f"Basic {token}"})
    assert r.status_code == 401


def test_auth_enabled_blocks_without_credentials(monkeypatch):
    monkeypatch.setenv("BASIC_AUTH_USERNAME", _AUTH_ENV["BASIC_AUTH_USERNAME"])
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", _AUTH_ENV["BASIC_AUTH_PASSWORD"])
    r = client.get("/")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"].startswith("Basic")
    # API 与 docs 同样设防
    assert client.get("/api/categories").status_code == 401
    assert client.get("/docs").status_code == 401


def test_auth_health_stays_open(monkeypatch):
    """探活路径不设防（无业务数据，供 HF/uptime 检查）。"""
    monkeypatch.setenv("BASIC_AUTH_USERNAME", _AUTH_ENV["BASIC_AUTH_USERNAME"])
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", _AUTH_ENV["BASIC_AUTH_PASSWORD"])
    assert client.get("/health").status_code == 200


def test_auth_accepts_correct_credentials(monkeypatch):
    monkeypatch.setenv("BASIC_AUTH_USERNAME", _AUTH_ENV["BASIC_AUTH_USERNAME"])
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", _AUTH_ENV["BASIC_AUTH_PASSWORD"])
    headers = _creds("partner", "s3cret-合同")
    assert client.get("/", headers=headers).status_code == 200
    assert client.get("/api/categories", headers=headers).status_code == 200


def test_auth_rejects_wrong_credentials(monkeypatch):
    monkeypatch.setenv("BASIC_AUTH_USERNAME", _AUTH_ENV["BASIC_AUTH_USERNAME"])
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", _AUTH_ENV["BASIC_AUTH_PASSWORD"])
    assert client.get("/", headers=_creds("partner", "wrong")).status_code == 401
    assert client.get("/", headers=_creds("nobody", "s3cret-合同")).status_code == 401


def test_auth_malformed_header_401(monkeypatch):
    monkeypatch.setenv("BASIC_AUTH_USERNAME", _AUTH_ENV["BASIC_AUTH_USERNAME"])
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", _AUTH_ENV["BASIC_AUTH_PASSWORD"])
    for header in ("Basic !!!not-base64!!!", "Bearer something", "Basic " + "??"):
        assert client.get("/", headers={"Authorization": header}).status_code == 401
