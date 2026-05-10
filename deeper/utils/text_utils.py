"""
Text processing utilities: tokenization, semantic chunking, cleaning.
"""
import re
from typing import List

import tiktoken

_ENCODER = None


def _get_encoder() -> tiktoken.Encoding:
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    return _ENCODER


def count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text))


def chunk_text(
    text: str,
    chunk_size: int = 800,
    overlap: int = 150,
) -> List[str]:
    """
    Semantic chunking: split by paragraphs and headings first,
    then fall back to token-based splitting for oversized blocks.

    This keeps each chunk semantically coherent — a paragraph or
    section stays together rather than being cut mid-sentence.
    """
    # Split into semantic blocks: headings, paragraphs, list items
    blocks = _split_into_blocks(text)

    if not blocks:
        return _token_chunk(text, chunk_size, overlap)

    chunks: List[str] = []
    current_parts: List[str] = []
    current_tokens = 0

    for block in blocks:
        block_tokens = count_tokens(block)

        # Block is too large on its own — split it by tokens
        if block_tokens > chunk_size:
            # Flush current buffer first
            if current_parts:
                chunks.append("\n\n".join(current_parts))
                current_parts = []
                current_tokens = 0
            # Split the oversized block
            sub_chunks = _token_chunk(block, chunk_size, overlap)
            chunks.extend(sub_chunks)
            continue

        # Adding this block would overflow — flush and start new chunk
        if current_tokens + block_tokens > chunk_size and current_parts:
            chunks.append("\n\n".join(current_parts))
            # Overlap: keep last part in next chunk for context
            if overlap > 0 and current_parts:
                last = current_parts[-1]
                current_parts = [last]
                current_tokens = count_tokens(last)
            else:
                current_parts = []
                current_tokens = 0

        current_parts.append(block)
        current_tokens += block_tokens

    # Flush remaining
    if current_parts:
        chunks.append("\n\n".join(current_parts))

    return chunks if chunks else [text]


def _split_into_blocks(text: str) -> List[str]:
    """
    Split text into semantic blocks:
    - Markdown headings (# ## ###)
    - Double newline paragraphs
    - Single newline lines (for plain text)
    """
    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Split on double newlines (paragraph boundaries)
    raw_blocks = re.split(r"\n{2,}", text)

    blocks: List[str] = []
    for block in raw_blocks:
        block = block.strip()
        if not block:
            continue
        # If block contains markdown headings, split further
        if re.search(r"^#{1,3}\s", block, re.MULTILINE):
            sub = re.split(r"(?=^#{1,3}\s)", block, flags=re.MULTILINE)
            blocks.extend(s.strip() for s in sub if s.strip())
        else:
            blocks.append(block)

    return blocks


def _token_chunk(text: str, chunk_size: int, overlap: int) -> List[str]:
    """Pure token-based chunking — fallback for oversized blocks."""
    encoder = _get_encoder()
    tokens = encoder.encode(text)

    if len(tokens) <= chunk_size:
        return [text]

    chunks: List[str] = []
    start = 0
    while start < len(tokens):
        end = min(start + chunk_size, len(tokens))
        chunks.append(encoder.decode(tokens[start:end]))
        if end == len(tokens):
            break
        start += chunk_size - overlap

    return chunks


def clean_html_text(text: str) -> str:
    """Remove excess whitespace and non-printable characters."""
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\x09\x0A\x0D\x20-\x7E\u00A0-\uFFFF]", "", text)
    return text.strip()


def truncate_text(text: str, max_tokens: int = 4000) -> str:
    encoder = _get_encoder()
    tokens = encoder.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return encoder.decode(tokens[:max_tokens])


def extract_urls_from_text(text: str) -> List[str]:
    return re.findall(r"https?://[^\s\]>\"')]*", text)


def format_sources(sources: List[str]) -> str:
    if not sources:
        return "_Источники недоступны._"
    return "\n".join(f"{i+1}. {url}" for i, url in enumerate(sources))


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:100]
