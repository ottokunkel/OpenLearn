"""Textual TUI for running benchmarks and inspecting DoclingDocument results."""

import json
import os
from pathlib import Path
from threading import Event

import yaml

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    Markdown,
    ProgressBar,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from benchmark.tracker import ApiUsageTracker
from worker.converter import CancelledError

CONFIG_PATH = "benchmark/config.yaml"
RESULTS_PATH = "benchmark/data/results.json"
DOCUMENTS_DIR = "benchmark/data/documents"


class BenchmarkTUI(App):
    TITLE = "Docling Benchmark"
    SUB_TITLE = "DoclingDocumentConversionWorker"

    CSS = """
    #config-summary {
        margin: 1 2;
        padding: 1 2;
        background: $surface;
        height: auto;
    }
    #run-controls {
        height: auto;
        margin: 0 2;
        align: left middle;
    }
    #run-btn { margin: 0 1 0 0; }
    #progress-section {
        height: auto;
        margin: 0 2;
    }
    #progress-label { height: 1; }
    #progress-bar { margin: 0 0 1 0; }
    #request-status {
        height: 1;
        margin: 0 2;
        color: $text-muted;
    }
    #output-log {
        margin: 0 2 1 2;
        height: 1fr;
        border: solid $primary;
    }
    #results-table { height: 1fr; }
    #doc-sidebar {
        width: 32;
        height: 1fr;
        border-right: solid $primary;
    }
    #doc-files { height: 1fr; }
    #doc-main {
        width: 1fr;
        height: 1fr;
    }
    #doc-content { height: 1fr; }
    #doc-tree {
        width: 1fr;
        height: 1fr;
        border: solid $primary;
    }
    #doc-right {
        width: 1fr;
        height: 1fr;
    }
    #doc-markdown {
        height: 1fr;
        border: solid $accent;
        overflow-y: auto;
    }
    #doc-detail {
        height: auto;
        max-height: 14;
        padding: 1 2;
        background: $surface;
        border: solid $accent;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("1", "tab_run", "Run"),
        Binding("2", "tab_results", "Results"),
        Binding("3", "tab_document", "Document"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent():
            with TabPane("Run [1]", id="run-tab"):
                yield Static(id="config-summary")
                with Horizontal(id="run-controls"):
                    yield Button("Run Benchmark", id="run-btn", variant="primary")
                with Vertical(id="progress-section"):
                    yield Static("", id="progress-label")
                    yield ProgressBar(id="progress-bar", total=100, show_eta=False)
                    yield Static("", id="request-status")
                yield RichLog(id="output-log", highlight=True, markup=True)

            with TabPane("Results [2]", id="results-tab"):
                yield DataTable(id="results-table", zebra_stripes=True, cursor_type="row")

            with TabPane("Document [3]", id="doc-tab"):
                with Horizontal():
                    with Vertical(id="doc-sidebar"):
                        yield ListView(id="doc-files")
                    with Vertical(id="doc-main"):
                        with Horizontal(id="doc-content"):
                            yield Tree("Document", id="doc-tree")
                            with Vertical(id="doc-right"):
                                yield Markdown("*Select a document from the sidebar*", id="doc-markdown")
                                yield Static("", id="doc-detail")
        yield Footer()

    def on_mount(self) -> None:
        self._load_config_summary()
        self._load_results()
        self._refresh_doc_files()
        self.query_one("#progress-section").display = False
        self._current_doc_dict: dict | None = None
        self._cancel_event: Event = Event()
        self._is_running: bool = False

    # ── Tab switching ──

    def action_tab_run(self) -> None:
        self.query_one(TabbedContent).active = "run-tab"

    def action_tab_results(self) -> None:
        self.query_one(TabbedContent).active = "results-tab"

    def action_tab_document(self) -> None:
        self.query_one(TabbedContent).active = "doc-tab"

    # ── Config summary ──

    def _load_config_summary(self) -> None:
        widget = self.query_one("#config-summary", Static)
        try:
            with open(CONFIG_PATH) as f:
                cfg = yaml.safe_load(f)
            pdfs = ", ".join(
                e.get("label", e["path"]) for e in cfg.get("pdfs", [])
            )
            models = cfg.get("models", [])
            model_lines = []
            for m in models:
                name = m.get("name", "unknown")
                url = m.get("base_url", "")
                key_src = "api_key" if m.get("api_key") else m.get("api_key_env", "")
                pricing = m.get("pricing", {})
                in_rate = pricing.get("input_cost_per_mtok", 0)
                out_rate = pricing.get("output_cost_per_mtok", 0)
                model_lines.append(
                    f"  {name}  ({url})  key:{key_src}  ${in_rate}/${out_rate} per M tok"
                )
            models_str = "\n".join(model_lines) if model_lines else "  (none)"
            widget.update(
                f"[b]Config:[/b]  {CONFIG_PATH}\n"
                f"[b]PDFs:[/b]    {pdfs}\n"
                f"[b]Models:[/b]\n{models_str}"
            )
        except FileNotFoundError:
            widget.update(f"[red]Config not found: {CONFIG_PATH}[/red]")

    # ── Run benchmark ──

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-btn":
            if self._is_running:
                self._cancel_event.set()
                self.query_one("#run-btn", Button).label = "Stopping..."
                self.query_one("#run-btn", Button).disabled = True
            else:
                self._cancel_event.clear()
                self._do_run_benchmark()

    def _log(self, text: str) -> None:
        log = self.query_one("#output-log", RichLog)
        self.call_from_thread(log.write, text)

    def _set_progress(self, current: int, total: int, label: str) -> None:
        bar = self.query_one("#progress-bar", ProgressBar)
        lbl = self.query_one("#progress-label", Static)
        self.call_from_thread(bar.update, total=total, progress=current)
        self.call_from_thread(lbl.update, label)

    @work(thread=True, exclusive=True)
    def _do_run_benchmark(self) -> None:
        from benchmark.runner import load_config, build_settings, resolve_api_key, run_single

        self._is_running = True
        btn = self.query_one("#run-btn", Button)
        progress_section = self.query_one("#progress-section")
        log = self.query_one("#output-log", RichLog)
        self.call_from_thread(log.clear)
        self.call_from_thread(setattr, btn, "label", "Stop")
        self.call_from_thread(setattr, btn, "variant", "error")
        self.call_from_thread(setattr, progress_section, "display", True)

        try:
            cfg = load_config(CONFIG_PATH)
        except Exception as e:
            self._log(f"[red]Failed to load config: {e}[/red]")
            self._finish_run()
            return

        models = cfg.get("models", [])
        pdfs = cfg.get("pdfs", [])
        output_cfg = cfg.get("output", {})
        documents_dir = output_cfg.get("documents_dir", DOCUMENTS_DIR)

        # Build (model_cfg, pdf_entry) job list
        jobs = []
        for model_cfg in models:
            api_key = resolve_api_key(model_cfg)
            if not api_key:
                key_src = model_cfg.get("api_key_env", "api_key")
                self._log(f"[yellow]Skipping {model_cfg.get('name')}: no key ({key_src})[/yellow]")
                continue
            for pdf_entry in pdfs:
                if not os.path.isfile(pdf_entry["path"]):
                    self._log(f"[red]PDF not found: {pdf_entry['path']}[/red]")
                    continue
                jobs.append((model_cfg, pdf_entry))

        if not jobs:
            self._log("[yellow]No jobs to run[/yellow]")
            self._finish_run()
            return

        total_jobs = len(jobs)
        results = []
        cancel = self._cancel_event

        try:
            for i, (model_cfg, pdf_entry) in enumerate(jobs):
                if cancel.is_set():
                    self._log("[yellow]Benchmark cancelled by user[/yellow]")
                    break

                model_name = model_cfg.get("name", "unknown")
                pdf_path = pdf_entry["path"]
                label = pdf_entry.get("label", Path(pdf_path).stem)
                pricing = model_cfg.get("pricing", {})

                self._set_progress(
                    i, total_jobs,
                    f"[{i + 1}/{total_jobs}] {model_name} on {label}",
                )

                def on_api_call(call_info):
                    self._log(
                        f"  API call #{len(tracker.calls)}: "
                        f"{call_info.input_tokens:,} in / {call_info.output_tokens:,} out  "
                        f"({call_info.duration:.1f}s)  "
                        f"running cost: ${tracker.total_cost:.4f}"
                    )

                def on_retry(attempt, max_retries, msg, wait):
                    self._log(
                        f"  [yellow]Retry {attempt}/{max_retries}: "
                        f"{msg} — waiting {wait:.1f}s[/yellow]"
                    )

                def on_error(status_code, msg, will_retry):
                    tag = "[yellow]" if will_retry else "[red]"
                    action = "will retry" if will_retry else "giving up"
                    self._log(
                        f"  {tag}HTTP {status_code}: {msg} ({action})[/{tag[1:]}"
                    )

                def on_status(in_flight, completed, failed):
                    status = self.query_one("#request-status", Static)
                    parts = [
                        f"In-flight: [bold]{in_flight}[/bold]",
                        f"Done: [green]{completed}[/green]",
                    ]
                    if failed:
                        parts.append(f"Failed: [red]{failed}[/red]")
                    self.call_from_thread(status.update, " | ".join(parts))

                tracker = ApiUsageTracker(pricing=pricing, on_api_call=on_api_call)
                settings = build_settings(model_cfg)

                self._log(f"[bold]{model_name} on {label}...[/bold]")

                try:
                    result = run_single(
                        pdf_path,
                        label,
                        settings,
                        tracker,
                        output_dir=documents_dir,
                        on_retry=on_retry,
                        on_error=on_error,
                        on_status=on_status,
                        cancel_event=cancel,
                    )
                    results.append(result)
                    self._log(
                        f"[green]  Done in {result['wall_time_seconds']}s "
                        f"— ${result['estimated_cost_usd']:.4f}  "
                        f"({result['page_count']} pages, "
                        f"{result['api_calls']} API calls)[/green]"
                    )
                except CancelledError:
                    self._log("[yellow]  Cancelled[/yellow]")
                    break
                except Exception as e:
                    self._log(f"[red]  FAILED: {e}[/red]")
                    results.append({"pdf": label, "model": model_name, "error": str(e)})

            if cancel.is_set():
                self._set_progress(0, 1, "Cancelled")
                self.call_from_thread(self.notify, "Benchmark cancelled", severity="warning")
            else:
                self._set_progress(total_jobs, total_jobs, "Complete")
                self.call_from_thread(self.notify, "Benchmark complete!", severity="information")

            json_path = output_cfg.get("results_json", RESULTS_PATH)
            if json_path and results:
                os.makedirs(os.path.dirname(json_path), exist_ok=True)
                with open(json_path, "w") as f:
                    json.dump({"runs": results}, f, indent=2)
                self._log(f"\nResults saved to {json_path}")

            ok_runs = [r for r in results if "error" not in r]
            total_cost = sum(r["estimated_cost_usd"] for r in ok_runs)
            total_time = sum(r["wall_time_seconds"] for r in ok_runs)
            self._log(
                f"\n[bold]Summary: {len(ok_runs)}/{total_jobs} succeeded  "
                f"Total: {total_time:.1f}s  ${total_cost:.4f}[/bold]"
            )
        except Exception as e:
            self._log(f"[red]Unexpected error: {e}[/red]")
            self.call_from_thread(self.notify, str(e), severity="error")
        finally:
            self._finish_run()

    def _finish_run(self) -> None:
        self._is_running = False
        btn = self.query_one("#run-btn", Button)
        status = self.query_one("#request-status", Static)
        self.call_from_thread(setattr, btn, "disabled", False)
        self.call_from_thread(setattr, btn, "label", "Run Benchmark")
        self.call_from_thread(setattr, btn, "variant", "primary")
        self.call_from_thread(status.update, "")
        self.call_from_thread(self._load_results)
        self.call_from_thread(self._refresh_doc_files)

    # ── Results table ──

    def _load_results(self) -> None:
        table = self.query_one("#results-table", DataTable)
        table.clear(columns=True)

        try:
            with open(RESULTS_PATH) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        runs = [r for r in data.get("runs", []) if "error" not in r]
        if not runs:
            return

        table.add_columns(
            "PDF", "Model", "Time", "Pages",
            "API Calls", "Input Tok", "Output Tok", "Cost",
        )

        for r in runs:
            table.add_row(
                r.get("pdf", ""),
                r["model"],
                f"{r['wall_time_seconds']}s",
                str(r["page_count"]),
                str(r["api_calls"]),
                f"{r['input_tokens']:,}",
                f"{r['output_tokens']:,}",
                f"${r['estimated_cost_usd']:.4f}",
            )

    # ── Document viewer ──

    def _refresh_doc_files(self) -> None:
        listview = self.query_one("#doc-files", ListView)
        listview.clear()

        docs_path = Path(DOCUMENTS_DIR)
        if not docs_path.exists():
            return

        for f in sorted(docs_path.glob("*.json")):
            listview.append(ListItem(Label(f.stem), name=str(f)))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "doc-files":
            return
        file_path = event.item.name
        if file_path:
            self._load_document(file_path)

    def _load_document(self, file_path: str) -> None:
        try:
            with open(file_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            self.query_one("#doc-detail", Static).update(f"Error: {e}")
            return

        chunks = data.get("chunks", [])
        markdown = data.get("markdown", "")

        # Build tree from hierarchical chunks
        tree_widget = self.query_one("#doc-tree", Tree)
        tree_widget.clear()
        tree_widget.root.set_label(Path(file_path).stem)
        tree_widget.root.data = None
        self._build_chunk_tree(tree_widget.root, chunks)
        tree_widget.root.expand_all()

        # Show markdown
        md_widget = self.query_one("#doc-markdown", Markdown)
        md_widget.update(markdown or "*No markdown content*")

        self.query_one("#doc-detail", Static).update(
            f"{len(chunks)} chunks  |  Navigate tree with arrow keys"
        )

    def _build_chunk_tree(self, root, chunks: list[dict]):
        """Build tree from HierarchicalChunker output using heading hierarchy."""
        node_map: dict[tuple, object] = {}

        for chunk in chunks:
            headings = chunk.get("headings") or []
            label = chunk.get("label", "")
            page = chunk.get("page")
            text = chunk.get("text", "")

            # Create/find heading branch nodes
            parent = root
            for i, heading in enumerate(headings):
                key = tuple(headings[: i + 1])
                if key not in node_map:
                    node = parent.add(heading, data={"type": "heading", "heading": heading})
                    node_map[key] = node
                parent = node_map[key]

            # Add chunk as leaf
            page_tag = f" [p.{page}]" if page is not None else ""
            preview = (text[:80] + "...") if len(text) > 80 else text
            leaf_label = f"({label}){page_tag} {preview}" if label else f"{page_tag} {preview}"
            parent.add_leaf(leaf_label.strip(), data={"type": "chunk", "chunk": chunk})

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if event.control.id != "doc-tree":
            return

        node_data = event.node.data
        detail = self.query_one("#doc-detail", Static)

        if not node_data or not isinstance(node_data, dict):
            detail.update("")
            return

        if node_data.get("type") == "heading":
            detail.update(f"[b]Heading:[/b] {node_data['heading']}")
            return

        chunk = node_data.get("chunk", {})
        lines = []

        headings = chunk.get("headings")
        if headings:
            lines.append(f"[b]Path:[/b]    {' > '.join(headings)}")
        if chunk.get("label"):
            lines.append(f"[b]Label:[/b]   {chunk['label']}")
        if chunk.get("page") is not None:
            lines.append(f"[b]Page:[/b]    {chunk['page']}")

        text = chunk.get("text", "")
        if text:
            preview = text[:500] + ("..." if len(text) > 500 else "")
            lines.append(f"[b]Text:[/b]\n{preview}")

        detail.update("\n".join(lines))


def main():
    from dotenv import load_dotenv

    load_dotenv()
    app = BenchmarkTUI()
    app.run()
