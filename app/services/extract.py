"""Document text extraction.

Prefer Docling for PDF/Word when installed; always support plain text / .txt
so the demo works without heavy deps.

Fail-closed 原则（2026-09-07 外部审计 P0）：解析不出可用文本时**报错**，
绝不把二进制乱码当文本送去审查——假成功会产出「看似正式、实际全错」的
审查结果，比直接失败危害大得多。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

TEXT_SUFFIXES = {".txt", ".md", ".text"}
DOC_SUFFIXES = {".pdf", ".docx", ".doc"}

# OLE2 复合文档魔数（旧版 .doc / xls / ppt 等）
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# 乱码判定阈值：控制字符（除 \t\n\r）+ NUL 占比超过 3% 即视为二进制/损坏。
# 正常中英文合同控制字符占比≈0；ZIP/PDF 二进制通常 >10%
_GARBAGE_RATIO = 0.03


class ExtractionError(ValueError):
    """解析失败（用户可见的错误消息）。"""


def extract_text(filename: str, raw: bytes) -> str:
    """Extract plain text from uploaded bytes. Raises ExtractionError on failure."""
    suffix = Path(filename).suffix.lower()

    if suffix in DOC_SUFFIXES:
        text = _try_docling(filename, raw)
        if text is not None and not _looks_like_garbage(text):
            return text

        # 旧版 .doc：容器级二进制格式，python-docx/pypdf 都读不了，
        # 无 docling 时与其吐乱码不如明确指路
        if suffix == ".doc" and raw[:8] == _OLE_MAGIC:
            raise ExtractionError(
                "检测到旧版 .doc 格式，当前环境无法解析。请将文件另存为 .docx 或 "
                ".txt 后重新上传。"
            )

        # Fallbacks
        if suffix == ".docx":
            text = _try_python_docx(raw)
            if text is not None and not _looks_like_garbage(text):
                return text
        if suffix == ".pdf":
            text = _try_pypdf(raw)
            if text is not None and not _looks_like_garbage(text):
                return text

        raise ExtractionError(
            f"无法从 {suffix} 文件中提取文本：文件可能已损坏、为扫描件（无文字层）"
            "或为加密文件。请确认文件可正常打开后重新上传，或改传 .txt 文本。"
        )

    text = _decode_bytes(raw)
    if _looks_like_garbage(text):
        raise ExtractionError(
            "文件内容不是可读文本（疑似二进制或损坏文件），已停止审查。"
            "请上传文本版合同（.txt / .md / .docx / .pdf）。"
        )
    return text


def _looks_like_garbage(text: str) -> bool:
    """控制字符/NUL 占比超阈值 → 二进制或损坏文件的解码产物。"""
    if not text or not text.strip():
        return True
    bad = sum(
        1 for ch in text if (ord(ch) < 32 and ch not in "\t\n\r") or ord(ch) == 0x7F
    )
    return bad / len(text) > _GARBAGE_RATIO


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
    """docx 文本提取：段落 + 表格（外部审计 P0：正文写在表格里的合同此前会解析为空）。"""
    try:
        import io
        from docx import Document  # type: ignore

        doc = Document(io.BytesIO(raw))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    t = cell.text.strip()
                    if t:
                        parts.append(t)
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
