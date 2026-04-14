"""Docling VLM pipeline wiring. Thin wrapper around DocumentConverter."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import VlmConvertOptions, VlmPipelineOptions
from docling.datamodel.vlm_engine_options import ApiVlmEngineOptions, VlmEngineType
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.pipeline.vlm_pipeline import VlmPipeline

from .config import Config


def build_converter(cfg: Config) -> DocumentConverter:
    vlm_options = VlmConvertOptions.from_preset(
        "granite_docling",
        engine_options=ApiVlmEngineOptions(
            runtime_type=VlmEngineType.API,
            url=cfg.vlm_endpoint_url,
            params={
                "model": cfg.vlm_model_name,
                "temperature": 0.0,
                # max_model_len on the vLLM server is 8192 (see modal_app.py:42).
                # Leave ~half for the image prompt; 4096 output covers dense pages.
                "max_tokens": 4096,
                "skip_special_tokens": False,
            },
            timeout=cfg.vlm_timeout_s,
        ),
    )
    pipeline_options = VlmPipelineOptions(
        vlm_options=vlm_options,
        enable_remote_services=True,
    )
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=VlmPipeline,
                pipeline_options=pipeline_options,
            ),
        }
    )


def convert_pdf(
    converter: DocumentConverter,
    pdf_bytes: bytes,
) -> tuple[str, dict[str, Any], int]:
    """Run the Docling VLM pipeline over ``pdf_bytes``.

    Returns (markdown, doctags_json_dict, page_count).
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(pdf_bytes)
        tmp.flush()
        result = converter.convert(Path(tmp.name))

    doc = result.document
    markdown = doc.export_to_markdown()
    doctags = doc.export_to_dict()
    page_count = len(doc.pages) if hasattr(doc, "pages") else 0
    return markdown, doctags, page_count
