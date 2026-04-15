from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline

from ..artifacts import PipelineResult
from ._common import (
    docling_json_artifact,
    markdown_artifact,
    page_count,
    run_converter,
)


class StandardCpuPipeline:
    name = "standard_cpu"

    def __init__(self) -> None:
        opts = PdfPipelineOptions()
        opts.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CPU)
        opts.do_ocr = True
        opts.do_table_structure = True
        self._converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_cls=StandardPdfPipeline,
                    pipeline_options=opts,
                )
            }
        )

    def convert(self, pdf_bytes: bytes) -> PipelineResult:
        doc = run_converter(self._converter, pdf_bytes).document
        return PipelineResult(
            artifacts=[markdown_artifact(doc), docling_json_artifact(doc)],
            page_count=page_count(doc),
        )
