"""Run a PDF through the Docling VLM pipeline and dump debug artifacts.

Writes three files next to the input PDF under --out:
  - <stem>.doctags.json  — full DoclingDocument JSON
  - <stem>.md            — rendered markdown
  - <stem>.annotated.pdf — input pages rendered as images with layout bboxes overlaid

Usage:
  VLM_ENDPOINT_URL=https://.../v1/chat/completions \\
  uv run python scripts/inspect_pdf.py [pdf_path] [--out DIR] [--scale N]

Defaults:
  pdf_path = tests/fixtures/gaussians.pdf
  out      = tmp/inspect/
  scale    = 2 (render resolution multiplier)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pypdfium2 import PdfDocument

from docling_core.types.doc import CoordOrigin

from DoclingWorker.config import Config
from DoclingWorker.docling_runner import build_converter

# Per-label colors for the bbox overlay.
LABEL_COLORS = {
    "section_header": (220, 20, 60),
    "title": (220, 20, 60),
    "paragraph": (30, 144, 255),
    "text": (30, 144, 255),
    "list_item": (0, 128, 128),
    "caption": (255, 140, 0),
    "footnote": (128, 128, 128),
    "page_header": (128, 128, 128),
    "page_footer": (128, 128, 128),
    "table": (148, 0, 211),
    "picture": (34, 139, 34),
    "formula": (255, 20, 147),
    "code": (70, 130, 180),
}
DEFAULT_COLOR = (0, 0, 0)


def _build_cfg(vlm_url: str) -> Config:
    return Config(
        supabase_url="",
        supabase_service_role_key="",
        vlm_endpoint_url=vlm_url,
        vlm_model_name=os.environ.get("VLM_MODEL_NAME", "ibm-granite/granite-docling-258M"),
        vlm_timeout_s=int(os.environ.get("VLM_TIMEOUT_S", "600")),
        poll_interval_s=5.0,
        visibility_timeout_s=300,
        batch_size=1,
        log_level="INFO",
    )


def _collect_items(doc):
    """Yield (label, page_no, bbox) for every item that has a bbox."""
    for group in ("texts", "tables", "pictures"):
        items = getattr(doc, group, None) or []
        for item in items:
            label = getattr(item, "label", None) or group.rstrip("s")
            label = str(label.value) if hasattr(label, "value") else str(label)
            for prov in getattr(item, "prov", None) or []:
                bbox = getattr(prov, "bbox", None)
                page_no = getattr(prov, "page_no", None)
                if bbox is None or page_no is None:
                    continue
                yield label, page_no, bbox


def _bbox_to_pixels(bbox, page_height_pts: float, scale: float) -> tuple[float, float, float, float]:
    """Convert a DoclingDocument bbox (PDF points, either coord origin) to PIL pixel rect."""
    if bbox.coord_origin == CoordOrigin.BOTTOMLEFT:
        # Flip Y: PIL is top-left origin with y growing downward.
        top_pts = page_height_pts - bbox.t
        bottom_pts = page_height_pts - bbox.b
    else:
        top_pts = bbox.t
        bottom_pts = bbox.b
    # Normalize so top < bottom
    y0, y1 = sorted([top_pts, bottom_pts])
    x0, x1 = sorted([bbox.l, bbox.r])
    return (x0 * scale, y0 * scale, x1 * scale, y1 * scale)


def _annotate_pdf(input_pdf: Path, doc, out_pdf: Path, scale: float = 2.0) -> None:
    pdf = PdfDocument(str(input_pdf))
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", int(10 * scale))
    except OSError:
        font = ImageFont.load_default()

    # Bucket items by page number (1-indexed as DoclingDocument uses)
    per_page: dict[int, list] = {}
    for label, page_no, bbox in _collect_items(doc):
        per_page.setdefault(page_no, []).append((label, bbox))

    pages_rendered: list[Image.Image] = []
    for idx in range(len(pdf)):
        page = pdf[idx]
        image = page.render(scale=scale).to_pil().convert("RGB")
        draw = ImageDraw.Draw(image)
        page_h_pts = page.get_size()[1]  # (width, height) in points

        for label, bbox in per_page.get(idx + 1, []):
            color = LABEL_COLORS.get(label, DEFAULT_COLOR)
            rect = _bbox_to_pixels(bbox, page_h_pts, scale)
            draw.rectangle(rect, outline=color, width=max(1, int(scale)))
            # Label tag above the box
            tx, ty = rect[0], max(0, rect[1] - int(14 * scale))
            draw.text((tx, ty), label, fill=color, font=font)

        pages_rendered.append(image)

    if not pages_rendered:
        raise RuntimeError("no pages rendered")

    pages_rendered[0].save(
        out_pdf,
        save_all=True,
        append_images=pages_rendered[1:],
        format="PDF",
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    default_pdf = repo_root / "tests" / "fixtures" / "gaussians.pdf"

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", nargs="?", default=str(default_pdf), type=Path)
    ap.add_argument("--out", default=str(repo_root / "tmp" / "inspect"), type=Path)
    ap.add_argument("--scale", default=2.0, type=float)
    args = ap.parse_args()

    vlm_url = os.environ.get("VLM_ENDPOINT_URL")
    if not vlm_url:
        print("ERROR: VLM_ENDPOINT_URL env var required.", file=sys.stderr)
        return 2

    if not args.pdf.exists():
        print(f"ERROR: pdf not found: {args.pdf}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.pdf.stem

    print(f"[inspect] converting {args.pdf.name} via {vlm_url}")
    cfg = _build_cfg(vlm_url)
    converter = build_converter(cfg)
    result = converter.convert(args.pdf)
    doc = result.document
    print(f"[inspect] pages={len(doc.pages)} texts={len(doc.texts or [])} "
          f"tables={len(doc.tables or [])} pictures={len(doc.pictures or [])}")

    # Write JSON + markdown
    doctags_path = args.out / f"{stem}.doctags.json"
    md_path = args.out / f"{stem}.md"
    doctags_path.write_text(json.dumps(doc.export_to_dict(), indent=2))
    md_path.write_text(doc.export_to_markdown())
    print(f"[inspect] wrote {doctags_path} ({doctags_path.stat().st_size} bytes)")
    print(f"[inspect] wrote {md_path} ({md_path.stat().st_size} bytes)")

    # Annotated PDF
    annotated_path = args.out / f"{stem}.annotated.pdf"
    _annotate_pdf(args.pdf, doc, annotated_path, scale=args.scale)
    print(f"[inspect] wrote {annotated_path} ({annotated_path.stat().st_size} bytes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
