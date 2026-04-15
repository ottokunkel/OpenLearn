import json
import tempfile
from pathlib import Path
from typing import Any

from docling.document_converter import DocumentConverter

from ..artifacts import Artifact


def run_converter(converter: DocumentConverter, pdf_bytes: bytes) -> Any:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(pdf_bytes)
        tmp.flush()
        return converter.convert(Path(tmp.name))


def markdown_artifact(doc: Any) -> Artifact:
    return Artifact(
        name="markdown",
        content=doc.export_to_markdown().encode("utf-8"),
        content_type="text/markdown",
        extension="md",
    )


def docling_json_artifact(doc: Any) -> Artifact:
    return Artifact(
        name="docling_json",
        content=json.dumps(doc.export_to_dict()).encode("utf-8"),
        content_type="application/json",
        extension="docling.json",
    )


def doctags_artifact(doc: Any) -> Artifact:
    return Artifact(
        name="doctags",
        content=doc.export_to_doctags().encode("utf-8"),
        content_type="application/xml",
        extension="doctags.xml",
    )


def page_count(doc: Any) -> int:
    return len(doc.pages) if hasattr(doc, "pages") else 0
