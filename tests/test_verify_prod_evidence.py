# -*- coding: utf-8 -*-
"""生产证据核验脚本 · URL 保全红线测试（外审 P1 销项）。

--live 不得重写凭据文件的协议或端口：https://host:8443 必须原样请求、
http://host:8080 也必须原样请求；其他协议立即失败（防隐式降级明文发凭据）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import tools.m65.verify_prod_evidence as vpe


def _cred_file(tmp_path: Path, url: str) -> Path:
    p = tmp_path / "cred.txt"
    p.write_text(f"url={url}\nuser=u\npassword=p\n", encoding="utf-8")
    return p


def test_https_url_kept_verbatim(tmp_path: Path, monkeypatch) -> None:
    """https://host:8443 必须原样请求（不得降级重写为 http://host:8080）。"""
    seen: list[str] = []

    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {"status": "done", "items": [{"id": "payment", "status": "通过"}]}

    def fake_get(url: str, **_kw):
        seen.append(url)
        return _Resp()

    monkeypatch.setattr(vpe.requests, "get", fake_get)
    base, _auth = vpe._load_credentials(_cred_file(tmp_path, "https://host:8443"))
    assert base == "https://host:8443", f"BASE 被重写: {base}"
    vpe.live_refetch([{"filename": "f.docx", "review_id": "r1"}], base, ("u", "p"), [])
    assert seen == ["https://host:8443/api/review/r1"], f"请求 URL 被改写: {seen}"


def test_http_8080_url_kept_verbatim(tmp_path: Path, monkeypatch) -> None:
    """http://host:8080（当前部署形态）必须原样请求。"""
    seen: list[str] = []

    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {"status": "done", "items": []}

    def fake_get(url: str, **_kw):
        seen.append(url)
        return _Resp()

    monkeypatch.setattr(vpe.requests, "get", fake_get)
    base, _auth = vpe._load_credentials(_cred_file(tmp_path, "http://host:8080"))
    assert base == "http://host:8080"
    vpe.live_refetch([{"filename": "f.docx", "review_id": "r1"}], base, ("u", "p"), [])
    assert seen == ["http://host:8080/api/review/r1"]


def test_unknown_protocol_rejected(tmp_path: Path) -> None:
    """非 http/https 协议立即失败，绝不携带凭据发起请求。"""
    with pytest.raises(ValueError, match="http"):
        vpe._load_credentials(_cred_file(tmp_path, "ftp://host:21"))
