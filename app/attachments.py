"""Extract readable text from email attachments (PDF, DOCX, plain text)."""
import io


def extract_text(filename: str, mime_type: str, data: bytes, max_chars: int = 8000) -> str:
    name = (filename or "").lower()
    mt = (mime_type or "").lower()
    try:
        if name.endswith(".pdf") or "pdf" in mt:
            return _pdf(data)[:max_chars]
        if name.endswith(".docx") or "wordprocessingml" in mt:
            return _docx(data)[:max_chars]
        if name.endswith((".txt", ".csv", ".md")) or mt.startswith("text/"):
            return data.decode("utf-8", errors="ignore")[:max_chars]
    except Exception as e:  # noqa: BLE001 - never let a bad attachment kill the run
        return f"[could not read attachment {filename}: {e}]"
    return ""  # unsupported type -> silently skipped


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts).strip()


def _docx(data: bytes) -> str:
    import docx
    document = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in document.paragraphs).strip()
