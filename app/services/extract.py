"""Document text extraction.

Prefer Docling for PDF/Word when installed; always support plain text / .txt
so the demo works without heavy deps.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

TEXT_SUFFIXES = {".txt", ".md", ".text"}
DOC_SUFFIXES = {".pdf", ".docx", ".doc"}


def extract_text(filename: str, raw: bytes) -> str:
    """Extract plain text from uploaded bytes."""
    suffix = Path(filename).suffix.lower()

    if suffix in TEXT_SUFFIXES or not suffix:
        return _decode_bytes(raw)

    if suffix in DOC_SUFFIXES:
        text = _try_docling(filename, raw)
        if text is not None:
            return text
        # Fallbacks
        if suffix == ".docx":
            text = _try_python_docx(raw)
            if text is not None:
                return text
        if suffix == ".pdf":
            text = _try_pypdf(raw)
            if text is not None:
                return text
        # Last resort: treat as text (may be garbage for binary)
        decoded = _decode_bytes(raw)
        if decoded.strip():
            logger.warning("Binary doc %s fell back to raw decode", filename)
            return decoded
        raise ValueError(
            f"无法解析 {suffix} 文件。请安装 docling，或上传 .txt 文本用于演示。"
        )

    # Unknown extension: try UTF-8 text
    return _decode_bytes(raw)


def _decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _try_docling(filename: str, raw: bytes) -> str | None:
    try:
        from docling.document_converter import DocumentConverter  # type: ignore
        import tempfile
        import os

        suffix = Path(filename).suffix or ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        try:
            converter = DocumentConverter()
            result = converter.convert(tmp_path)
            text = result.document.export_to_markdown()
            return text or None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001
        logger.info("Docling unavailable or failed: %s", exc)
        return None


def _try_python_docx(raw: bytes) -> str | None:
    try:
        import io
        from docx import Document  # type: ignore

        doc = Document(io.BytesIO(raw))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(parts) if parts else None
    except Exception as exc:  # noqa: BLE001
        logger.info("python-docx unavailable or failed: %s", exc)
        return None


def _try_pypdf(raw: bytes) -> str | None:
    try:
        import io
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(io.BytesIO(raw))
        parts = []
        for page in reader.pages:
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t)
        return "\n".join(parts) if parts else None
    except Exception as exc:  # noqa: BLE001
        logger.info("pypdf unavailable or failed: %s", exc)
        return None
