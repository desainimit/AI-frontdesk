"""Docling structure is preserved through chunks and page provenance."""

import re
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO

import tiktoken


@dataclass(frozen=True)
class ParsedChunk:
    text: str
    section: str | None
    subsection: str | None
    page_number: int | None
    token_count: int
    checksum: str
    source_metadata: dict[str, object]


def parse_document(name: str, body: bytes) -> list[ParsedChunk]:
    # Docling is an optional worker dependency; importing it never slows API startup.
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.document_converter import DocumentConverter
    from docling_core.transforms.chunker.doc_chunk import DocChunk
    from docling_core.transforms.chunker.hierarchical_chunker import HierarchicalChunker

    extension = name.rsplit(".", 1)[-1].lower()
    allowed = {"pdf": InputFormat.PDF, "docx": InputFormat.DOCX, "md": InputFormat.MD}
    if extension not in allowed:
        raise ValueError("Unsupported document format")
    converter = DocumentConverter(allowed_formats=[allowed[extension]])
    document = converter.convert(
        DocumentStream(name=name, stream=BytesIO(body)),
        max_file_size=25_000_000,
        max_num_pages=100,
    ).document
    tokenizer = tiktoken.get_encoding("cl100k_base")
    chunks: list[ParsedChunk] = []
    for raw in HierarchicalChunker().chunk(dl_doc=document):
        if not isinstance(raw, DocChunk):
            continue
        headings = raw.meta.headings or []
        section = str(headings[0])[:300] if headings else None
        subsection = str(headings[-1])[:300] if len(headings) > 1 else None
        pages = sorted({prov.page_no for item in raw.meta.doc_items for prov in item.prov})
        page_number = pages[0] if pages else None
        references = [str(item.self_ref) for item in raw.meta.doc_items][:20]
        # Split only exceptional oversized semantic elements at sentence boundaries.
        paragraphs = re.split(r"(?<=[.!?।])\s+|\n\n+", raw.text.strip())
        current = ""
        for paragraph in [*paragraphs, ""]:
            proposed = f"{current} {paragraph}".strip()
            if paragraph and len(tokenizer.encode(proposed)) <= 1000:
                current = proposed
                continue
            if current:
                count = len(tokenizer.encode(current))
                chunks.append(
                    ParsedChunk(
                        current,
                        section,
                        subsection,
                        page_number,
                        count,
                        sha256(current.encode()).hexdigest(),
                        {"pages": pages[:20], "doc_items": references},
                    )
                )
            current = paragraph
            if len(tokenizer.encode(current)) > 1000:
                # An indivisible element is rejected instead of embedding an
                # overlong input or silently discarding source provenance.
                raise ValueError("Document contains an oversized element")
    if not chunks or len(chunks) > 1000:
        raise ValueError("Document has no usable content or too many chunks")
    return chunks
