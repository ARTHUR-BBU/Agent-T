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

        # 旧版 .doc：容器级二进制格式，python-docx/pypdf 都读不了。
        # 优先 catdoc（用户需求 2026-09-15：支持 .doc 结尾；服务器镜像已装，
        # 本地/CI 未装时 shutil.which 守卫快速跳过），不可用再友好指路
        if suffix == ".doc" and raw[:8] == _OLE_MAGIC:
            text = _try_catdoc(raw)
            if text is not None and not _looks_like_garbage(text):
                return text
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


def _iter_docx_blocks(doc):
    """按 document body 文档顺序产出段落与表格（交错），避免「先全部段落再全部表格」。"""
    from docx.oxml.ns import qn  # type: ignore
    from docx.table import Table  # type: ignore
    from docx.text.paragraph import Paragraph  # type: ignore

    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def _table_text_parts(table) -> list[str]:
    parts: list[str] = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            t = cell.text.strip()
            if t:
                cells.append(t)
        if cells:
            # 同行单元格用制表符连接，保留表内列关系
            parts.append("\t".join(cells))
    return parts


def _try_python_docx(raw: bytes) -> str | None:
    """docx 文本提取：按文档顺序交错段落与表格（可信度 P1 F05）。

    旧实现先扫完全部段落再扫全部表格，会把夹在条款中间的付款表甩到文末，
    导致条款归属错位。
    """
    try:
        import io
        from docx import Document  # type: ignore
        from docx.table import Table  # type: ignore
        from docx.text.paragraph import Paragraph  # type: ignore

        doc = Document(io.BytesIO(raw))
        parts: list[str] = []
        for block in _iter_docx_blocks(doc):
            if isinstance(block, Paragraph):
                t = block.text.strip()
                if t:
                    parts.append(t)
            elif isinstance(block, Table):
                parts.extend(_table_text_parts(block))
        return "\n".join(parts) if parts else None
    except Exception as exc:  # noqa: BLE001
        logger.info("python-docx unavailable or failed: %s", exc)
        return None


def _try_catdoc(raw: bytes) -> str | None:
    """旧版 .doc（OLE2）文本提取：catdoc -d utf-8（外部命令，服务器镜像安装）。

    catdoc 对中文 .doc 的支持优于 antiword；不可用（未安装/解析失败/超时）
    返回 None，调用方回退友好指路。fail-closed：任何异常都不产出半吊子文本。
    """
    import shutil
    import subprocess
    import tempfile
    import os

    if shutil.which("catdoc") is None:
        logger.info("catdoc not installed; .doc fallback unavailable")
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        try:
            proc = subprocess.run(
                ["catdoc", "-d", "utf-8", tmp_path],
                capture_output=True,
                timeout=30,
            )
            if proc.returncode != 0:
                logger.info("catdoc failed (rc=%s): %s", proc.returncode, proc.stderr[:200])
                return None
            return proc.stdout.decode("utf-8", errors="replace") or None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001
        logger.info("catdoc unavailable or failed: %s", exc)
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
