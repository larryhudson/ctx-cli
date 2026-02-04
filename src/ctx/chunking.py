"""Text chunking utilities."""

from __future__ import annotations

import tiktoken

from ctx.models import DocumentChunk

# Default tokenizer (cl100k_base is used by text-embedding-3-small and similar)
_tokenizer: tiktoken.Encoding | None = None


def get_tokenizer() -> tiktoken.Encoding:
    """Get the tokenizer instance."""
    global _tokenizer  # noqa: PLW0603
    if _tokenizer is None:
        _tokenizer = tiktoken.get_encoding("cl100k_base")
    return _tokenizer


def count_tokens(text: str) -> int:
    """Count the number of tokens in a text string."""
    return len(get_tokenizer().encode(text))


def chunk_text(
    text: str,
    max_tokens: int = 500,
    overlap_tokens: int = 50,
) -> list[DocumentChunk]:
    """Split text into chunks based on token count.

    Args:
        text: The text to chunk.
        max_tokens: Maximum tokens per chunk.
        overlap_tokens: Number of tokens to overlap between chunks.

    Returns:
        List of DocumentChunk objects with content and chunk_index.
    """
    tokenizer = get_tokenizer()
    tokens = tokenizer.encode(text)

    if len(tokens) <= max_tokens:
        return [DocumentChunk(content=text, chunk_index=0)]

    chunks: list[DocumentChunk] = []
    start = 0
    chunk_index = 0

    while start < len(tokens):
        # Get chunk tokens
        end = min(start + max_tokens, len(tokens))
        chunk_tokens = tokens[start:end]

        # Decode back to text
        chunk_content = tokenizer.decode(chunk_tokens)
        chunks.append(DocumentChunk(content=chunk_content, chunk_index=chunk_index))

        # Move start position, accounting for overlap
        start = end - overlap_tokens if end < len(tokens) else end
        chunk_index += 1

    return chunks


def chunk_by_paragraphs(
    text: str,
    max_tokens: int = 500,
    overlap_tokens: int = 50,
) -> list[DocumentChunk]:
    """Split text into chunks, trying to preserve paragraph boundaries.

    This is often better for semantic search as it keeps related
    content together.

    Args:
        text: The text to chunk.
        max_tokens: Maximum tokens per chunk.
        overlap_tokens: Number of tokens to overlap between chunks.

    Returns:
        List of DocumentChunk objects.
    """
    # Split into paragraphs
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    if not paragraphs:
        return [DocumentChunk(content=text, chunk_index=0)]

    chunks: list[DocumentChunk] = []
    current_chunk: list[str] = []
    current_tokens = 0
    chunk_index = 0

    for paragraph in paragraphs:
        para_tokens = count_tokens(paragraph)

        # If single paragraph exceeds max, chunk it separately
        if para_tokens > max_tokens:
            # First, flush current chunk if any
            if current_chunk:
                chunks.append(
                    DocumentChunk(
                        content="\n\n".join(current_chunk),
                        chunk_index=chunk_index,
                    )
                )
                chunk_index += 1
                current_chunk = []
                current_tokens = 0

            # Chunk the large paragraph
            para_chunks = chunk_text(paragraph, max_tokens, overlap_tokens)
            for pc in para_chunks:
                chunks.append(
                    DocumentChunk(
                        content=pc.content,
                        chunk_index=chunk_index,
                    )
                )
                chunk_index += 1
            continue

        # Check if adding this paragraph would exceed max
        if current_tokens + para_tokens > max_tokens and current_chunk:
            # Flush current chunk
            chunks.append(
                DocumentChunk(
                    content="\n\n".join(current_chunk),
                    chunk_index=chunk_index,
                )
            )
            chunk_index += 1

            # Start new chunk, optionally with overlap from last paragraph
            if overlap_tokens > 0 and current_chunk:
                last_para = current_chunk[-1]
                if count_tokens(last_para) <= overlap_tokens:
                    current_chunk = [last_para, paragraph]
                    current_tokens = count_tokens(last_para) + para_tokens
                else:
                    current_chunk = [paragraph]
                    current_tokens = para_tokens
            else:
                current_chunk = [paragraph]
                current_tokens = para_tokens
        else:
            current_chunk.append(paragraph)
            current_tokens += para_tokens

    # Flush remaining
    if current_chunk:
        chunks.append(
            DocumentChunk(
                content="\n\n".join(current_chunk),
                chunk_index=chunk_index,
            )
        )

    return chunks
