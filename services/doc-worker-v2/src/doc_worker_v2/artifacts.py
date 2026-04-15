from dataclasses import dataclass


@dataclass(frozen=True)
class Artifact:
    name: str          # stable key: "markdown" | "docling_json" | "doctags" | ...
    content: bytes
    content_type: str  # MIME type
    extension: str     # trailing filename extension, e.g. "md", "json", "doctags.json"


@dataclass(frozen=True)
class PipelineResult:
    artifacts: list[Artifact]
    page_count: int
