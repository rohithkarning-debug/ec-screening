"""Structure-aware chunking (L1).

Why not fixed-size chunks: the Schedule is a table, and the General Condition
is a separate note printed after it. Fixed windows would split a Schedule row
across chunks, or glue the General Condition onto the last rows of the table,
where a search for "cement plant" would never surface it.

Strategy:
1. Schedule rows and the General Condition come from `corpus/clauses.yaml`,
   where each is pasted verbatim from the PDF with its clause ID and page.
   One clause = one chunk. The General Condition is tagged `type=modifier`.
2. Amendments and office memoranda (PDFs) are split by paragraph, packed into
   chunks of about `max_chars`, with a small overlap. A paragraph that looks
   like it amends the General Condition or a Schedule item is tagged
   `type=amendment` and keeps a `clause_id` reference when one is found.
3. A section headed "General Condition" found inside a PDF is extracted as
   its own `modifier` chunk as well, so it is retrievable even before the
   clause file is filled in.

Each chunk ID is a hash of (doc_id, clause_id, text), which makes ingestion
idempotent: unchanged chunks keep their IDs and are not re-embedded.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

GC_HEADING = re.compile(r"general\s+condition", re.I)
NEXT_HEADING = re.compile(r"^\s*(specific\s+condition|note[s]?\b|schedule\b|appendix|annexure)", re.I | re.M)
CLAUSE_REF = re.compile(r"\b(?:item|entry|serial)\s*(?:no\.?\s*)?(\d{1,2}\s*\(\s*[a-z]\s*\))", re.I)
AMEND_HINT = re.compile(r"\b(amend|substitut|omit|insert|in the schedule|general condition)", re.I)


@dataclass
class Chunk:
    doc_id: str
    text: str
    type: str
    clause_id: str = ""
    page: int = -1
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        h = hashlib.sha256(f"{self.doc_id}|{self.clause_id}|{self.text}".encode()).hexdigest()
        return f"{self.doc_id}:{self.clause_id or 'p'}:{h[:16]}"

    def metadata(self) -> dict:
        m = {"doc_id": self.doc_id, "type": self.type, "clause_id": self.clause_id,
             "page": self.page, "content_hash": self.id.rsplit(":", 1)[-1]}
        m.update({k: v for k, v in self.extra.items() if v is not None})
        return m


def normalize_clause_id(raw: str) -> str:
    return re.sub(r"\s+", "", raw).lower()


def clause_chunks(clauses: list[dict]) -> list[Chunk]:
    """One chunk per verified clause from clauses.yaml. Placeholders are skipped."""
    out = []
    for c in clauses:
        text = (c.get("text") or "").strip()
        if not text or "PASTE" in text.upper():
            continue
        out.append(Chunk(doc_id=c["doc_id"], text=text, type=c["type"],
                         clause_id=c["clause_id"], page=int(c.get("page") or -1),
                         extra={"doc_title": c.get("doc_title"), "sector": c.get("sector")}))
    return out


def _paragraphs(text: str, max_chars: int = 900) -> list[str]:
    """Blank-line paragraphs; any longer than max_chars are split at line, then sentence, ends."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        para = re.sub(r"[ \t]+", " ", para).strip()
        if not para:
            continue
        if len(para) <= max_chars:
            out.append(para)
            continue
        buf = ""
        for piece in re.split(r"(?<=[.;:])\s+|\n", para):
            if len(buf) + len(piece) + 1 > max_chars and buf:
                out.append(buf)
                buf = piece
            else:
                buf = (buf + " " + piece).strip()
        if buf:
            out.append(buf)
    return out


def extract_general_condition(pages: list[str]) -> tuple[str, int] | None:
    """Return (text, page) of a 'General Condition' section found in a PDF, if any."""
    for i, page in enumerate(pages):
        m = GC_HEADING.search(page)
        if not m:
            continue
        tail = page[m.start():] + "\n" + (pages[i + 1] if i + 1 < len(pages) else "")
        end = NEXT_HEADING.search(tail, pos=len("general condition"))
        section = tail[: end.start()] if end else tail[:2500]
        section = re.sub(r"[ \t]+", " ", section).strip()
        if len(section) > 60:
            return section, i + 1
    return None


def pdf_chunks(doc_id: str, doc_title: str, pages: list[str], max_chars: int = 900,
               overlap: int = 150) -> list[Chunk]:
    out: list[Chunk] = []
    gc = extract_general_condition(pages)
    if gc:
        out.append(Chunk(doc_id=doc_id, text=gc[0], type="modifier", clause_id="GC", page=gc[1],
                         extra={"doc_title": doc_title, "source": "pdf"}))
    for pno, page in enumerate(pages, start=1):
        buf = ""
        for para in _paragraphs(page, max_chars):
            if len(buf) + len(para) + 1 > max_chars and buf:
                out.append(_para_chunk(doc_id, doc_title, buf, pno))
                buf = buf[-overlap:] + " " + para
            else:
                buf = (buf + " " + para).strip()
        if buf:
            out.append(_para_chunk(doc_id, doc_title, buf, pno))
    return out


def _para_chunk(doc_id: str, doc_title: str, text: str, page: int) -> Chunk:
    ref = CLAUSE_REF.search(text)
    ctype = "amendment" if AMEND_HINT.search(text) else "paragraph"
    return Chunk(doc_id=doc_id, text=text, type=ctype,
                 clause_id=normalize_clause_id(ref.group(1)) if (ref and ctype == "amendment") else "",
                 page=page, extra={"doc_title": doc_title, "source": "pdf"})
