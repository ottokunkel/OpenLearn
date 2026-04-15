from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import VlmConvertOptions, VlmPipelineOptions
from docling.datamodel.vlm_engine_options import ApiVlmEngineOptions, VlmEngineType
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.pipeline.vlm_pipeline import VlmPipeline

from ..artifacts import PipelineResult
from ._common import (
    docling_json_artifact,
    doctags_artifact,
    markdown_artifact,
    page_count,
    run_converter,
)


class GraniteDoclingVlmPipeline:
    name = "granite_docling_vlm"

    def __init__(self, endpoint_url: str, model_name: str, timeout_s: int):
        vlm_options = VlmConvertOptions.from_preset(
            "granite_docling",
            engine_options=ApiVlmEngineOptions(
                runtime_type=VlmEngineType.API,
                url=endpoint_url,
                params={
                    "model": model_name,
                    "temperature": 0.0,
                    "max_tokens": 4096,
                    "skip_special_tokens": False,
                },
                timeout=timeout_s,
            ),
        )
        self._converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_cls=VlmPipeline,
                    pipeline_options=VlmPipelineOptions(
                        vlm_options=vlm_options,
                        enable_remote_services=True,
                    ),
                )
            }
        )

    def convert(self, pdf_bytes: bytes) -> PipelineResult:
        doc = run_converter(self._converter, pdf_bytes).document
        return PipelineResult(
            artifacts=[
                markdown_artifact(doc),
                docling_json_artifact(doc),
                doctags_artifact(doc),
            ],
            page_count=page_count(doc),
        )
