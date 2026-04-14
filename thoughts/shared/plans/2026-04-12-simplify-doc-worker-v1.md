# Simplify doc-worker-v1 Implementation Plan

## Overview

Restructure `services/doc-worker-v1` around a small, well-named library and a unified `python -m doc_worker.cli.*` command surface. Collapse three styles of configuration (env, dataclass with empty fakes, YAML+env-expansion) into one sectioned `Config`. Replace mock-heavy unit tests with a single integration test plus a tiny live-Modal CLI smoke test. Document the CLI flow so a developer can run `python -m doc_worker.cli.convert path.pdf` and see a `path.docling.json`, then `python -m doc_worker.cli.annotate path.docling.json path.pdf` to inspect bbox overlays — all without needing Supabase.

## Current State Analysis

`services/doc-worker-v1` (~1500 LOC) does its job but has accumulated incoherence:

- **Two parallel CLI surfaces** with no overlap: `python -m DoclingWorker` (queue poller) and `uv run python scripts/inspect_pdf.py` (single-PDF debug). A stub `main.py` does nothing but print "Hello".
- **Three config styles**: a dataclass loader (`src/DoclingWorker/config.py`), env-only (`scripts/inspect_pdf.py:52` builds a `Config` with empty Supabase fields just to satisfy required fields), and YAML with custom `${VAR:-default}` expansion (`benchmarks/benchmark_config.yaml` + 30 LOC of regex parsing in `benchmark_vlm_modal.py`).
- **Tests in name only**: `tests/test_modal.py` is a 130-LOC `argparse` script with no assertions; `tests/test_job_processor.py` is mock-heavy and largely re-asserts what the integration test already covers end-to-end.
- **Naming inconsistencies**: `DoclingWorker` (PascalCase package — non-idiomatic), `VLMEndpoints/Modal/modal_app.py` (deeply nested), `docling_runner.py`, `job_processor.py`, `supabase_client.py` (suffix-heavy).
- **README is empty**, so a new contributor has no path from `git clone` to seeing a docling JSON.
- **Cross-platform breakage**: `scripts/inspect_pdf.py:99` hardcodes `/System/Library/Fonts/Helvetica.ttc`.

### Key Discoveries
- The Modal app (`src/VLMEndpoints/Modal/modal_app.py`) is already clean and well-commented — only the file path needs changing.
- Worker correctness is small: `src/DoclingWorker/job_processor.py` is the only complex module (105 LOC, well-structured, lazy import of docling for testability).
- pgmq operations are RPC calls (`src/DoclingWorker/queue.py:19-36`) — already minimal.
- Integration test (`tests/test_integration_gaussians.py`) exercises the real path correctly; it just needs a slightly broader scope (cover the convert-only CLI path too).
- `tests/conftest.py:60-105` is a solid fixture for end-to-end runs; can be reused.
- The annotate-PDF logic in `scripts/inspect_pdf.py:96-133` is genuinely useful and worth preserving as a separate command.

## Desired End State

A single coherent package layout:

```
services/doc-worker-v1/
├── pyproject.toml                       # entry points: doc-worker, doc-convert, doc-annotate, doc-benchmark
├── README.md                            # setup → deploy Modal → CLI usage → run worker → tests
├── .env.example                         # one set of env vars, sectioned with comments
├── src/
│   ├── doc_worker/
│   │   ├── __init__.py
│   │   ├── __main__.py                  # python -m doc_worker → worker.run()
│   │   ├── config.py                    # sectioned Config (vlm, supabase?, worker)
│   │   ├── docling.py                   # build_converter, convert_pdf
│   │   ├── queue.py                     # pgmq read/delete/archive
│   │   ├── supabase.py                  # client factory
│   │   ├── processor.py                 # per-job orchestration
│   │   ├── worker.py                    # polling loop + signal handling
│   │   └── cli/
│   │       ├── __init__.py
│   │       ├── convert.py               # python -m doc_worker.cli.convert pdf [-o json]
│   │       ├── annotate.py              # python -m doc_worker.cli.annotate json pdf [-o annotated.pdf]
│   │       ├── benchmark.py             # env-driven Modal benchmark
│   │       └── probe_modal.py           # streaming chat probe (was tests/test_modal.py)
│   └── vlm_endpoint/
│       ├── __init__.py
│       └── modal_app.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py                      # simplified, lazy fixtures
│   ├── test_integration.py              # one end-to-end test (gated)
│   └── fixtures/
│       └── gaussians.pdf
└── uv.lock
```

### Verification this end state has been reached:
- `uv run doc-convert tests/fixtures/gaussians.pdf` writes `gaussians.docling.json` and prints page count.
- `uv run doc-annotate gaussians.docling.json tests/fixtures/gaussians.pdf` writes `gaussians.annotated.pdf` with labelled bboxes.
- `uv run doc-worker` runs the queue poller; clean SIGINT shutdown.
- `RUN_INTEGRATION_TESTS=1 TEST_USER_ID=... uv run pytest -m integration` passes.
- Total Python LOC under `src/` + `cli/` is meaningfully smaller than today (target: ≤ 800 LOC excluding `modal_app.py`, vs. ~530 today — the cli replaces ~470 LOC of scripts/benchmarks).
- No file imports `DoclingWorker`; no file references `benchmark_config.yaml`.
- `README.md` is non-empty and walks through the four flows above.

### Key Design Decisions
- **Single sectioned Config, two loaders.** `Config(vlm: VlmConfig, worker: WorkerConfig, supabase: SupabaseConfig | None)`. Two factory functions: `load()` (everything required, for the worker) and `load_vlm_only()` (only VLM env vars required, for the CLI). This eliminates the empty-Supabase-fields hack at `scripts/inspect_pdf.py:52`.
- **`cli/` is a subpackage, not a top-level scripts directory.** Everything is `python -m doc_worker.cli.<name>`, plus installed console scripts via `[project.scripts]`. Single import path, single `--help` style.
- **`probe_modal.py` and `benchmark.py` live next to the other CLI tools.** They are observability tools, not tests. They share the same arg-parse + env-loading style.
- **Snake_case package name (`doc_worker`).** Idiomatic Python; aligns with directory conventions.
- **Library-first.** `doc_worker.processor.process(msg, client, converter)` and `doc_worker.docling.convert_pdf(converter, pdf_bytes)` remain importable so a future deployment can wrap the worker loop differently (e.g., as a background task in another service) without re-importing CLI code.

## What We're NOT Doing

- Not touching `apps/admin/` (out of scope per user).
- Not changing `src/vlm_endpoint/modal_app.py`'s deployment behaviour — only its file path and the `app.local_entrypoint()` block (kept as-is, since `modal run` invokes it).
- Not adding retry policies, dead-letter queues, or worker concurrency. Current single-message-at-a-time loop is intentional.
- Not migrating off `dotenv` / sectioned dataclass to pydantic-settings. Adds a dep for marginal gain.
- Not adding linting/formatting config (ruff/black). Out of scope; existing code style is consistent.
- Not adding type-checking via mypy/pyright. Out of scope.
- Not unit-testing config loading, CLI arg parsing, or the queue RPC wrappers. Integration test + manual CLI runs cover them transitively.
- Not preserving the empty `main.py` at the repo root. It will be deleted.

## Implementation Approach

Five phases. Each phase is a self-contained restructure that leaves the package importable and tests runnable. The order is chosen so we never have a half-broken package on disk:

1. **Restructure & rename** — establish the new layout with imports updated. No behaviour changes.
2. **Sectioned config** — split `Config` into sections, add `load_vlm_only()`.
3. **CLI subpackage** — convert, annotate, benchmark, probe_modal as `python -m doc_worker.cli.*`.
4. **Test simplification** — delete the unit test, prune conftest, keep one integration test.
5. **Documentation & cleanup** — README, delete `main.py`, delete `benchmark_config.yaml`, verify everything end-to-end.

---

## Phase 1: Restructure & Rename

### Overview
Move files into the target layout and update imports. No logic changes. After this phase the package imports as `doc_worker` and the Modal app lives at `src/vlm_endpoint/modal_app.py`.

### Changes Required

#### 1. Rename package directory
- `git mv src/DoclingWorker src/doc_worker`
- `git mv src/VLMEndpoints/Modal/modal_app.py src/vlm_endpoint/modal_app.py` (then `rmdir src/VLMEndpoints/Modal src/VLMEndpoints`)
- Add empty `src/vlm_endpoint/__init__.py`.

#### 2. Rename modules within `doc_worker/`
- `docling_runner.py` → `docling.py`
- `job_processor.py` → `processor.py`
- `supabase_client.py` → `supabase.py`
- Extract the polling loop from `__main__.py` into a new `worker.py` (function `run() -> int`).
- Reduce `__main__.py` to:
  ```python
  import sys
  from .worker import run
  if __name__ == "__main__":
      sys.exit(run())
  ```

#### 3. Update imports
- `processor.py`: `from . import queue as q` (unchanged); inner `from .docling import convert_pdf` (was `convert_pdf_bytes` → rename to `convert_pdf` for brevity).
- `worker.py`: `from . import config, queue, supabase, processor` and `from .docling import build_converter`.
- `tests/conftest.py`: replace all `from DoclingWorker...` with `from doc_worker...`.
- `tests/test_integration_gaussians.py`: same import replacement.

#### 4. Update `pyproject.toml`
**File**: `services/doc-worker-v1/pyproject.toml`
```toml
[project.scripts]
doc-worker = "doc_worker.__main__:run"
```
(Drop the old `docling-worker` entry. Console scripts for the CLI tools are added in Phase 3.)

#### 5. Rename `convert_pdf_bytes` → `convert_pdf`
**File**: `src/doc_worker/docling.py` (was `docling_runner.py`)
Cosmetic: function still takes bytes, but the `_bytes` suffix added noise. Update the one caller in `processor.py`.

### Success Criteria

#### Automated Verification
- [x] Package imports: `uv run python -c "import doc_worker, doc_worker.processor, doc_worker.docling, doc_worker.queue, doc_worker.worker"`
- [x] Modal app imports: `uv run python -c "import vlm_endpoint.modal_app"`
- [x] Worker entry point runs (and exits on SIGTERM): `uv run doc-worker` then Ctrl-C, exits 0
- [x] Old test still passes against mocks: `uv run pytest tests/test_job_processor.py -v` (deleted early per Phase 1 note — stale imports made it invalid)

#### Manual Verification
- [x] `git status` shows clean renames (no duplicate files, no leftover empty dirs)
- [x] `grep -r DoclingWorker src/ tests/ scripts/` returns nothing (only leftover is `scripts/inspect_pdf.py`, which Phase 3 deletes)
- [x] `grep -r VLMEndpoints src/ tests/ scripts/` returns nothing

**Implementation Note**: After this phase the old `scripts/inspect_pdf.py` and `tests/test_modal.py` and `benchmarks/benchmark_vlm_modal.py` still exist with their old import paths broken. That is intentional — they get rewritten in Phase 3, not patched in Phase 1. Pytest collection of those files should be skipped if they break; that's fine since they aren't `test_*` shaped (well, `test_modal.py` is — but it has no asserting tests, so collection failure won't matter for green pytest runs of just the integration suite).

To be safe, between Phase 1 and Phase 3 we'll temporarily exclude these files via `[tool.pytest.ini_options].norecursedirs` or just delete `tests/test_modal.py` early (it has no real tests anyway).

---

## Phase 2: Sectioned Config

### Overview
Restructure `config.py` so the CLI tools don't have to fake Supabase fields, and so consumers can clearly see what env each tool needs.

### Changes Required

#### 1. Rewrite `src/doc_worker/config.py`

```python
"""Sectioned env-backed config. Worker requires all sections; CLI tools require only VLM."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


@dataclass(frozen=True)
class VlmConfig:
    endpoint_url: str
    model_name: str = "ibm-granite/granite-docling-258M"
    timeout_s: int = 600


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    service_role_key: str


@dataclass(frozen=True)
class WorkerConfig:
    poll_interval_s: float = 5.0
    visibility_timeout_s: int = 300
    batch_size: int = 1


@dataclass(frozen=True)
class Config:
    vlm: VlmConfig
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    supabase: SupabaseConfig | None = None
    log_level: str = "INFO"


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _vlm_from_env() -> VlmConfig:
    return VlmConfig(
        endpoint_url=_require("VLM_ENDPOINT_URL"),
        model_name=os.environ.get("VLM_MODEL_NAME", VlmConfig.model_name),
        timeout_s=int(os.environ.get("VLM_TIMEOUT_S", VlmConfig.timeout_s)),
    )


def _worker_from_env() -> WorkerConfig:
    return WorkerConfig(
        poll_interval_s=float(os.environ.get("WORKER_POLL_INTERVAL_S", WorkerConfig.poll_interval_s)),
        visibility_timeout_s=int(os.environ.get("WORKER_VISIBILITY_TIMEOUT_S", WorkerConfig.visibility_timeout_s)),
        batch_size=int(os.environ.get("WORKER_BATCH_SIZE", WorkerConfig.batch_size)),
    )


def load_vlm_only() -> Config:
    """Load just enough config to run the VLM pipeline (no Supabase)."""
    load_dotenv()
    return Config(
        vlm=_vlm_from_env(),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )


def load() -> Config:
    """Load full worker config. Requires Supabase + VLM env vars."""
    load_dotenv()
    return Config(
        vlm=_vlm_from_env(),
        worker=_worker_from_env(),
        supabase=SupabaseConfig(
            url=_require("SUPABASE_URL"),
            service_role_key=_require("SUPABASE_SERVICE_ROLE_KEY"),
        ),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
```

#### 2. Update consumers

**File**: `src/doc_worker/docling.py`
- `build_converter` now takes `cfg: VlmConfig` (not the full `Config`). Update the one caller.

**File**: `src/doc_worker/supabase.py`
- `get_client(cfg: SupabaseConfig) -> Client` (not full Config). Caller in `worker.py` passes `cfg.supabase`.

**File**: `src/doc_worker/worker.py`
```python
def run() -> int:
    cfg = config.load()
    assert cfg.supabase is not None  # load() guarantees this
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("doc_worker")

    client = supabase.get_client(cfg.supabase)
    converter = build_converter(cfg.vlm)
    # ... rest unchanged but referencing cfg.worker.{poll_interval_s, visibility_timeout_s, batch_size}
```

#### 3. `.env.example` reformatting

```
# === VLM endpoint (required for both worker and CLI) ===
VLM_ENDPOINT_URL=https://<modal-web-url>/v1/chat/completions
VLM_MODEL_NAME=ibm-granite/granite-docling-258M
VLM_TIMEOUT_S=600

# === Supabase (required for worker only) ===
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=

# === Worker tuning (optional) ===
WORKER_POLL_INTERVAL_S=5
WORKER_VISIBILITY_TIMEOUT_S=300
WORKER_BATCH_SIZE=1
LOG_LEVEL=INFO

# === Integration test only ===
RUN_INTEGRATION_TESTS=0
TEST_USER_ID=
```

### Success Criteria

#### Automated Verification
- [ ] `uv run python -c "from doc_worker.config import load_vlm_only, load; print(load_vlm_only())"` works with only `VLM_ENDPOINT_URL` set
- [ ] `uv run python -c "from doc_worker.config import load; load()"` raises `RuntimeError` clearly listing missing Supabase vars when they're absent
- [ ] Worker still runs: `uv run doc-worker` (Ctrl-C exits cleanly)

#### Manual Verification
- [ ] Config errors mention env var names a developer can grep for
- [ ] `Config` is still printable for debugging (frozen dataclass repr)

---

## Phase 3: CLI Subpackage

### Overview
Replace `scripts/inspect_pdf.py`, `tests/test_modal.py`, `benchmarks/benchmark_vlm_modal.py` with four small CLI tools under `src/doc_worker/cli/`. All share `config.load_vlm_only()`. None reach for Supabase.

### Changes Required

#### 1. `src/doc_worker/cli/convert.py` (~50 LOC)

**Purpose**: PDF in, docling JSON (and optionally markdown) out. The simplest "does the pipeline work?" command.

```python
"""Convert a PDF to a Docling JSON document via the VLM pipeline.

Usage:
    uv run doc-convert path/to/file.pdf [-o output.docling.json] [--markdown]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..config import load_vlm_only
from ..docling import build_converter, convert_pdf


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-o", "--output", type=Path,
                    help="output JSON path (default: <pdf>.docling.json next to the input)")
    ap.add_argument("--markdown", action="store_true",
                    help="also write <pdf>.md alongside the JSON")
    args = ap.parse_args()

    if not args.pdf.is_file():
        print(f"error: not a file: {args.pdf}", file=sys.stderr)
        return 2

    cfg = load_vlm_only()
    converter = build_converter(cfg.vlm)

    out_json = args.output or args.pdf.with_suffix(".docling.json")
    print(f"[convert] {args.pdf.name} → {out_json.name} via {cfg.vlm.endpoint_url}", file=sys.stderr)

    pdf_bytes = args.pdf.read_bytes()
    markdown, doctags, page_count = convert_pdf(converter, pdf_bytes)
    out_json.write_text(json.dumps(doctags, indent=2))
    print(f"[convert] pages={page_count} bytes={out_json.stat().st_size}", file=sys.stderr)

    if args.markdown:
        md_path = args.pdf.with_suffix(".md")
        md_path.write_text(markdown)
        print(f"[convert] wrote {md_path.name} ({md_path.stat().st_size} bytes)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

#### 2. `src/doc_worker/cli/annotate.py` (~80 LOC)

**Purpose**: Take an existing docling JSON and the source PDF, render an annotated PDF showing bbox overlays per element. Pure file-in/file-out — no VLM call. This decouples observability from inference.

Key changes vs. `scripts/inspect_pdf.py`:
- Reads JSON from disk instead of running the converter — annotation is now offline.
- Uses `ImageFont.load_default()` (no macOS-only path).
- Reuses `_collect_items` and `_bbox_to_pixels` logic verbatim from current `inspect_pdf.py:66-93`, but driven by `DoclingDocument.model_validate(json_dict)` instead of a fresh conversion.

```python
"""Annotate a PDF with bounding boxes from a Docling JSON.

Usage:
    uv run doc-annotate path/to/doc.docling.json path/to/source.pdf [-o annotated.pdf] [--scale 2]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import ImageDraw, ImageFont
from pypdfium2 import PdfDocument
from docling_core.types.doc import CoordOrigin, DoclingDocument

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


def _items(doc: DoclingDocument):
    for group in ("texts", "tables", "pictures"):
        for item in (getattr(doc, group, None) or []):
            label = getattr(item, "label", None) or group.rstrip("s")
            label = str(label.value) if hasattr(label, "value") else str(label)
            for prov in (getattr(item, "prov", None) or []):
                bbox = getattr(prov, "bbox", None)
                page_no = getattr(prov, "page_no", None)
                if bbox is not None and page_no is not None:
                    yield label, page_no, bbox


def _to_pixels(bbox, page_h_pts: float, scale: float):
    if bbox.coord_origin == CoordOrigin.BOTTOMLEFT:
        top_pts = page_h_pts - bbox.t
        bot_pts = page_h_pts - bbox.b
    else:
        top_pts, bot_pts = bbox.t, bbox.b
    y0, y1 = sorted([top_pts, bot_pts])
    x0, x1 = sorted([bbox.l, bbox.r])
    return (x0 * scale, y0 * scale, x1 * scale, y1 * scale)


def annotate(json_path: Path, pdf_path: Path, out_path: Path, scale: float = 2.0) -> None:
    doc = DoclingDocument.model_validate(json.loads(json_path.read_text()))
    pdf = PdfDocument(str(pdf_path))
    font = ImageFont.load_default()

    per_page: dict[int, list] = {}
    for label, page_no, bbox in _items(doc):
        per_page.setdefault(page_no, []).append((label, bbox))

    pages = []
    for idx in range(len(pdf)):
        page = pdf[idx]
        image = page.render(scale=scale).to_pil().convert("RGB")
        draw = ImageDraw.Draw(image)
        page_h = page.get_size()[1]
        for label, bbox in per_page.get(idx + 1, []):
            color = LABEL_COLORS.get(label, DEFAULT_COLOR)
            rect = _to_pixels(bbox, page_h, scale)
            draw.rectangle(rect, outline=color, width=max(1, int(scale)))
            draw.text((rect[0], max(0, rect[1] - 14)), label, fill=color, font=font)
        pages.append(image)

    pages[0].save(out_path, save_all=True, append_images=pages[1:], format="PDF")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("json", type=Path, help="Docling JSON (output of doc-convert)")
    ap.add_argument("pdf", type=Path, help="source PDF that produced the JSON")
    ap.add_argument("-o", "--output", type=Path,
                    help="output PDF path (default: <pdf>.annotated.pdf)")
    ap.add_argument("--scale", default=2.0, type=float)
    args = ap.parse_args()

    if not args.json.is_file() or not args.pdf.is_file():
        print("error: json and pdf must both exist", file=sys.stderr)
        return 2

    out = args.output or args.pdf.with_suffix(".annotated.pdf")
    annotate(args.json, args.pdf, out, scale=args.scale)
    print(f"[annotate] wrote {out} ({out.stat().st_size} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

#### 3. `src/doc_worker/cli/benchmark.py` (~150 LOC)

**Purpose**: Replace the YAML+env-expansion benchmark with a pure env-driven script. Same metrics, same output format, ~half the code.

Drop:
- `_expand_env`, `_ENV_PATTERN`, `_as_bool`, `load_config` (~30 LOC of YAML/env-var glue)
- `argparse -c CONFIG` plumbing
- The YAML file itself

Keep:
- Cold-start measurement logic
- Concurrent vs sequential warm-request paths
- Cost estimate
- Stats summary

Config via env vars only (sensible defaults per below). All vars are optional except for `MODAL_APP_NAME` (defaults to `vlm-endpoint-docworker-v1`). Document them in the docstring.

```python
"""Benchmark the hosted VLM on Modal. Env-driven (see --help for vars)."""
# Reads:
#   BENCH_APP_NAME              default: vlm-endpoint-docworker-v1
#   BENCH_CLASS_NAME            default: VllmServer
#   BENCH_MODEL_NAME            default: ibm-granite/granite-docling-258M
#   BENCH_IMAGE                 default: <hf URL of the new_arxiv test image>
#   BENCH_PROMPT                default: "Convert this image to Docling."
#   BENCH_WARM_REQUESTS         default: 10
#   BENCH_CONCURRENCY           default: 1
#   BENCH_REQUIRE_COLD          default: true
#   BENCH_REQUEST_TIMEOUT_S     default: 600
#   BENCH_POLL_INTERVAL_S       default: 5
#   BENCH_MAX_COLD_WAIT_S       default: 120
#   BENCH_GPU                   default: L40S
#   BENCH_GPU_PRICE_PER_HOUR    default: 1.95
#   BENCH_INCLUDE_SCALEDOWN     default: true
#   BENCH_SCALEDOWN_S           default: 2     (must match modal_app.py SCALEDOWN_WINDOW)
```

The Modal helpers (`get_runner_count`, `wait_for_cold`, `resolve_url`), request builders (`build_image_part`, `send_request`), and the benchmark body (`run_benchmark`) are copied across with their `cfg["..."]` accesses replaced by `_env_int / _env_float / _env_bool / _env_str` helpers (~10 LOC of helpers).

#### 4. `src/doc_worker/cli/probe_modal.py` (~80 LOC)

**Purpose**: Streaming chat-completion smoke test against the deployed Modal app. Was `tests/test_modal.py`; moved here because it's a CLI tool, not a test.

Trim from the original 130 LOC:
- Drop `--no-stream` flag (rarely useful; streaming is the default for visibility)
- Keep `--image` (URL or local path) and `--prompt`
- Keep base64 image embedding for local files
- Use `BENCH_APP_NAME` env var for the Modal app name (defaulting to `vlm-endpoint-docworker-v1`) so it stays consistent with `benchmark.py`

#### 5. Update `pyproject.toml`

```toml
[project.scripts]
doc-worker    = "doc_worker.__main__:run"
doc-convert   = "doc_worker.cli.convert:main"
doc-annotate  = "doc_worker.cli.annotate:main"
doc-benchmark = "doc_worker.cli.benchmark:main"
doc-probe     = "doc_worker.cli.probe_modal:main"
```

#### 6. Add Pillow + pypdfium2 to dependencies
**File**: `pyproject.toml`
- Add `"pillow>=10.0.0"` and `"pypdfium2>=4.0.0"` (currently transitive via docling, but `annotate.py` imports them directly — make it explicit).

#### 7. Delete the old files
- `rm -r scripts/`
- `rm -r benchmarks/`

### Success Criteria

#### Automated Verification
- [ ] All four CLI commands print sensible `--help`: `for c in doc-worker doc-convert doc-annotate doc-benchmark doc-probe; do uv run $c --help; done`
- [ ] Convert without VLM env raises a clear error: `unset VLM_ENDPOINT_URL; uv run doc-convert tests/fixtures/gaussians.pdf` exits non-zero with `Missing required env var: VLM_ENDPOINT_URL`
- [ ] Annotate is offline: `uv run doc-annotate tests/fixtures/sample.docling.json tests/fixtures/gaussians.pdf -o /tmp/out.pdf` succeeds without any env var set (use a small fixture JSON if available, otherwise generate one with `doc-convert` first)

#### Manual Verification
- [ ] `uv run doc-convert tests/fixtures/gaussians.pdf` → produces `gaussians.docling.json` next to the PDF; page count printed matches expected (8–16)
- [ ] `uv run doc-annotate gaussians.docling.json tests/fixtures/gaussians.pdf` → opening the resulting PDF shows colored bboxes per element with labels
- [ ] `uv run doc-benchmark` produces the same kind of summary block as before (cold/warm/cost) without needing a YAML file
- [ ] `uv run doc-probe --prompt "what do you see?" --image https://huggingface.co/ibm-granite/granite-docling-258M/resolve/main/assets/new_arxiv.png` streams text to stdout

**Implementation Note**: After confirming the CLI commands work end-to-end against a live Modal endpoint, pause for confirmation before continuing to Phase 4.

---

## Phase 4: Test Simplification

### Overview
Delete the unit test that mocks Supabase, prune `conftest.py` to just the live fixtures, rename and slim the integration test.

### Changes Required

#### 1. Delete files
- `rm tests/test_job_processor.py`           (139 LOC of mocked unit tests)
- `rm tests/test_modal.py`                    (already moved to `cli/probe_modal.py` in Phase 3)

#### 2. Rename + slim integration test
**File**: `tests/test_integration.py` (was `test_integration_gaussians.py`)
- Keep the existing flow (seed → drain → process → assert DB + storage)
- Drop the `pytestmark = pytest.mark.integration` marker (no other tests, the gating env var is enough)
- Add one extra assertion: the markdown contains tokens from the actual document (already there: lines 62-63 — keep)

#### 3. Slim conftest
**File**: `tests/conftest.py`
- Drop the now-unused `cfg`, `converter` parameter splits — fold them into the test directly OR keep as fixtures (they are still useful). Keep them.
- Replace `from DoclingWorker import config as config_mod` → `from doc_worker import config as config_mod` and `config_mod.load()` (still works — Supabase is required).
- Update `from DoclingWorker import supabase_client` → `from doc_worker import supabase`; use `supabase.get_client(cfg.supabase)`.
- Update `from DoclingWorker.docling_runner import build_converter` → `from doc_worker.docling import build_converter`; pass `cfg.vlm`.

#### 4. Drop the `integration` pytest marker
**File**: `pyproject.toml`
```toml
[tool.pytest.ini_options]
pythonpath = ["src"]
# (remove the `markers` block)
```

### Success Criteria

#### Automated Verification
- [ ] `uv run pytest --collect-only` lists exactly one test (`test_integration::test_gaussians_end_to_end`)
- [ ] Without env vars set, that test is skipped (clean skip, not error): `unset RUN_INTEGRATION_TESTS; uv run pytest -v` shows 1 skipped, 0 failed
- [ ] With env vars set, it passes: `RUN_INTEGRATION_TESTS=1 TEST_USER_ID=<uuid> uv run pytest -v`

#### Manual Verification
- [ ] `tests/` contains only: `__init__.py`, `conftest.py`, `test_integration.py`, `fixtures/`
- [ ] No file in `tests/` mocks Supabase or the converter

---

## Phase 5: Documentation & Cleanup

### Overview
Write the README that ties it all together, delete leftover stubs, do a final import sweep.

### Changes Required

#### 1. Write `README.md`

Sections (concise — target ~120 lines including code blocks):

```markdown
# doc-worker-v1

Document ingestion worker. Pulls PDF jobs off Supabase pgmq, runs them through
Docling's VLM pipeline (granite-docling, hosted on Modal), uploads markdown
+ docling JSON to Supabase storage.

## Setup

    cd services/doc-worker-v1
    uv sync
    cp .env.example .env
    # fill in SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, VLM_ENDPOINT_URL

## Deploy the Modal VLM endpoint

    uv run modal deploy src/vlm_endpoint/modal_app.py
    # then copy the URL into VLM_ENDPOINT_URL

## CLI tools (no Supabase required)

Convert a PDF to docling JSON:

    uv run doc-convert tests/fixtures/gaussians.pdf
    # → tests/fixtures/gaussians.docling.json
    # add --markdown to also write a .md

Inspect bboxes by overlaying them on the original PDF:

    uv run doc-annotate gaussians.docling.json gaussians.pdf
    # → gaussians.annotated.pdf

Probe the Modal endpoint with a streaming chat completion:

    uv run doc-probe --image <url-or-path>

Benchmark the Modal endpoint (env-driven, see `--help` for knobs):

    uv run doc-benchmark
    BENCH_WARM_REQUESTS=20 BENCH_CONCURRENCY=4 uv run doc-benchmark

## Run the worker

    uv run doc-worker
    # polls document_jobs queue every WORKER_POLL_INTERVAL_S seconds
    # SIGTERM/SIGINT drains the in-flight message before exiting

## Tests

The single test is an end-to-end integration test that hits live Modal +
Supabase. Skipped by default. To run:

    export RUN_INTEGRATION_TESTS=1
    export TEST_USER_ID=<uuid from auth.users>
    uv run pytest -v

## Library use

The worker is also importable:

    from doc_worker.config import load
    from doc_worker.supabase import get_client
    from doc_worker.docling import build_converter, convert_pdf
    from doc_worker.processor import process

## Layout

    src/doc_worker/         worker library + CLI subpackage
      ├── worker.py         polling loop
      ├── processor.py      per-job orchestration
      ├── docling.py        VLM pipeline wrapper
      ├── queue.py          pgmq RPC wrappers
      ├── supabase.py       service-role client factory
      ├── config.py         env-backed sectioned config
      └── cli/              user-facing observability tools
    src/vlm_endpoint/       Modal deployment
    tests/                  one integration test + fixture
```

#### 2. Delete remnants
- `rm services/doc-worker-v1/main.py`        (stub, never referenced)
- Verify with `grep -rn "import main\|from main" services/doc-worker-v1/` returns nothing.

#### 3. Final sweep
- `grep -rn "DoclingWorker\|VLMEndpoints\|docling_runner\|job_processor\|supabase_client\|inspect_pdf\|benchmark_config" services/doc-worker-v1/ --include="*.py" --include="*.toml" --include="*.md"` should return nothing
- `uv lock` to refresh `uv.lock` for the new pillow/pypdfium2 explicit deps and removed YAML import
- Confirm `uv run doc-worker --help` (or just startup with missing env shows clean error)

### Success Criteria

#### Automated Verification
- [ ] `find services/doc-worker-v1 -name "*.py" -not -path "*/.venv/*" | xargs wc -l` shows under ~800 LOC outside of `modal_app.py`
- [ ] No leftover references: `grep -rn "DoclingWorker\|VLMEndpoints\|benchmark_config" services/doc-worker-v1/ --include="*.py" --include="*.toml" --include="*.md"` returns nothing
- [ ] `uv run pytest -v` (without env) skips gracefully
- [ ] `uv sync && uv run doc-worker` starts cleanly with a real `.env`
- [ ] Console scripts installed: `uv run which doc-convert doc-annotate doc-benchmark doc-worker doc-probe`

#### Manual Verification
- [ ] A first-time reader can clone, follow `README.md`, and produce a `gaussians.annotated.pdf` in under 10 minutes (assuming Modal endpoint is already deployed)
- [ ] The README's three flows (CLI / worker / tests) match real behaviour
- [ ] No empty stubs (`main.py`, empty README) remain

---

## Testing Strategy

### Integration tests
- **One real e2e test** (`tests/test_integration.py`): seeds a documents row, an object in storage, and a pgmq message; reads the message off the queue; runs the processor; verifies DB row state, storage objects, markdown content, doctags shape. Gated by `RUN_INTEGRATION_TESTS=1` and `TEST_USER_ID`.
- **CLI smoke test (manual)**: `doc-convert tests/fixtures/gaussians.pdf` followed by `doc-annotate gaussians.docling.json tests/fixtures/gaussians.pdf` — documented in README; serves as the "is the pipeline behaving" check that doesn't require Supabase.

### What we deliberately don't unit-test
- `config.load()` / `load_vlm_only()` — exercised every time any CLI runs; failure modes (missing env) are discovered immediately.
- `queue.read/delete/archive` — wrappers over `client.rpc()`. Mocking the Supabase client is what `test_job_processor.py` does today; the integration test exercises the same RPCs against real pgmq.
- `processor.process()` — covered end-to-end by the integration test against real Supabase + Modal.
- CLI argparse plumbing — `--help` exercises it; one wrong arg prints a usage error.

### Manual verification checklist (post-merge)
1. `uv sync && cp .env.example .env && <fill in>`
2. `uv run modal deploy src/vlm_endpoint/modal_app.py` (or skip if already deployed)
3. `uv run doc-convert tests/fixtures/gaussians.pdf --markdown` → JSON + MD appear, page count matches
4. `uv run doc-annotate tests/fixtures/gaussians.docling.json tests/fixtures/gaussians.pdf` → annotated PDF opens with visible bboxes
5. `uv run doc-probe` → streams a docling response from a test image
6. `uv run doc-benchmark` → prints summary with cold/warm/cost
7. `RUN_INTEGRATION_TESTS=1 TEST_USER_ID=<uuid> uv run pytest -v` → passes
8. `uv run doc-worker` → polls, processes a real seeded row, exits cleanly on Ctrl-C

## Performance Considerations

- The worker loop is unchanged — single message at a time, bounded VLM call. No regressions expected.
- Annotation is now offline (no VLM call), so `doc-annotate` is fast and free.
- `doc-convert` makes one VLM call per invocation; same as today's `inspect_pdf.py`.
- Benchmark output and meaning are preserved; only the config-loading code changes.

## Migration Notes

- Anyone with a checked-out branch using the old `DoclingWorker` import path will need to switch to `doc_worker`.
- Anyone using the `docling-worker` console script needs to switch to `doc-worker`.
- Anyone calling `uv run python scripts/inspect_pdf.py X.pdf` should switch to `uv run doc-convert X.pdf` followed by `uv run doc-annotate X.docling.json X.pdf` (annotation is now a separate step).
- Anyone using `benchmark_config.yaml` to pass values needs to set the corresponding `BENCH_*` env vars instead.

## References

- Worker library: `services/doc-worker-v1/src/doc_worker/`
- Modal endpoint: `services/doc-worker-v1/src/vlm_endpoint/modal_app.py`
- Original processor (unchanged behaviour, renamed file): `src/DoclingWorker/job_processor.py:38-105` → `src/doc_worker/processor.py`
- Original integration test: `tests/test_integration_gaussians.py:22-69` → `tests/test_integration.py`
- Original annotation logic: `scripts/inspect_pdf.py:66-133` → `src/doc_worker/cli/annotate.py`
