from hashlib import sha256

from app.schemas.rag import SourceInput, TextChunk


def split_source(source: SourceInput, *, size: int = 1000, overlap: int = 150) -> list[TextChunk]:
    """Character-based chunks, preserving offsets into the original UTF-8 text."""
    if not 0 <= overlap < size:
        raise ValueError("Require 0 <= overlap < size")
    chunks = []
    for start in range(0, len(source.text), size - overlap):
        end = min(start + size, len(source.text))
        text = source.text[start:end]
        if text.strip():
            key = (
                f"{source.tenant_id}:{source.source_type}:{source.source_id}:"
                f"{source.version}:{start}:{text}"
            )
            chunks.append(
                TextChunk(
                    chunk_id="CHUNK-" + sha256(key.encode()).hexdigest()[:20],
                    text=text,
                    start=start,
                    end=end,
                )
            )
        if end == len(source.text):
            break
    return chunks
