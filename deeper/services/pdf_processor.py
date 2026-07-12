"""
PDF processing using PyMuPDF.
Extracts text, chunks it, generates an LLM summary, and stores it.
"""

import fitz  # PyMuPDF
from groq import Groq

from deeper.utils.logger import get_logger
from deeper.utils.text_utils import chunk_text, truncate_text

logger = get_logger("pdf_processor")

PDF_SUMMARY_SYSTEM = """You are an expert document analyst.
Analyze the provided document text and produce a structured summary in the SAME language as the document.

Structure your response as:
## Document Summary
[2-3 sentence overview]

## Key Points
[Bullet list of main points]

## Important Details
[Notable facts, data, or insights]

## Conclusion
[Final takeaway]"""


class PDFProcessor:
    def __init__(
        self,
        groq_api_key: str,
        primary_model: str,
        chunk_size: int = 800,
        chunk_overlap: int = 150,
        max_pdf_mb: int = 20,
    ) -> None:
        self.client = Groq(api_key=groq_api_key)
        self.primary_model = primary_model
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.max_pdf_bytes = max_pdf_mb * 1024 * 1024

    def extract_text(self, pdf_bytes: bytes) -> str:
        """Extract full text from PDF bytes."""
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages_text = []
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text("text")
            if text.strip():
                pages_text.append(f"--- Page {page_num + 1} ---\n{text}")
        full_text = "\n\n".join(pages_text)
        logger.info("Extracted {} chars from {} page PDF", len(full_text), len(doc))
        return full_text

    def _summarize_chunk(self, chunk: str, chunk_idx: int, total: int) -> str:
        """Summarize a single chunk with the LLM."""
        prompt = (
            f"This is chunk {chunk_idx}/{total} of a document. "
            f"Extract the key information from this section:\n\n{chunk}"
        )
        response = self.client.chat.completions.create(
            model=self.primary_model,
            messages=[
                {"role": "system", "content": "Extract key information concisely."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=600,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()

    def _final_summary(self, combined: str) -> str:
        """Generate the final structured summary from aggregated chunk summaries."""
        truncated = truncate_text(combined, max_tokens=6000)
        response = self.client.chat.completions.create(
            model=self.primary_model,
            messages=[
                {"role": "system", "content": PDF_SUMMARY_SYSTEM},
                {"role": "user", "content": truncated},
            ],
            max_tokens=2000,
            temperature=0.4,
        )
        return response.choices[0].message.content.strip()

    async def process(self, pdf_bytes: bytes, filename: str = "document.pdf") -> dict:
        """
        Full pipeline: validate → extract → chunk → summarize.

        Returns dict with: filename, page_count, char_count, summary.
        Raises ValueError on validation errors.
        """
        import asyncio

        # Validate size
        if len(pdf_bytes) > self.max_pdf_bytes:
            raise ValueError(
                f"PDF is too large ({len(pdf_bytes) / 1024 / 1024:.1f} MB). "
                f"Maximum allowed: {self.max_pdf_bytes // 1024 // 1024} MB."
            )

        # Extract text
        try:
            loop = asyncio.get_event_loop()
            full_text = await loop.run_in_executor(None, self.extract_text, pdf_bytes)
        except Exception as e:
            logger.error("PDF extraction failed: {}", e)
            raise ValueError(f"Could not extract text from PDF: {e}")

        if len(full_text.strip()) < 50:
            raise ValueError("PDF appears to be empty or contains only images (no extractable text).")

        # Open again for metadata
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page_count = len(doc)
        doc.close()

        # Chunk text
        chunks = chunk_text(full_text, self.chunk_size, self.chunk_overlap)
        logger.info("PDF split into {} chunks", len(chunks))

        # Summarize chunks (limit to first 20 to avoid rate limits)
        max_chunks = min(len(chunks), 20)
        chunk_summaries = []

        for i, chunk in enumerate(chunks[:max_chunks], 1):
            try:
                summary = await loop.run_in_executor(
                    None, self._summarize_chunk, chunk, i, max_chunks
                )
                chunk_summaries.append(summary)
            except Exception as e:
                logger.warning("Failed to summarize chunk {}: {}", i, e)

        # Final summary
        combined = "\n\n".join(chunk_summaries)
        try:
            final = await loop.run_in_executor(None, self._final_summary, combined)
        except Exception as e:
            logger.error("Final PDF summary failed: {}", e)
            final = combined[:3000]

        return {
            "filename": filename,
            "page_count": page_count,
            "char_count": len(full_text),
            "summary": final,
        }
