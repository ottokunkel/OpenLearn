"""Textual TUI for running the full integration pipeline."""

from pathlib import Path
from threading import Event

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    RichLog,
    Static,
)

from integration.pipeline import PipelineRunner, StageResult
from worker.config import Settings

STAGE_NAMES = [
    "Connect",
    "Create Document",
    "Enqueue Job",
    "Download PDF",
    "Convert PDF",
    "Embed & Write Chunks",
    "Update Status",
    "Verify",
    "Vector Search",
    "Cleanup",
]


class PipelineTUI(App):
    TITLE = "OpenLearn Pipeline"
    SUB_TITLE = "Integration Test"

    CSS = """
    #main {
        height: 1fr;
    }
    #left-panel {
        width: 44;
        border-right: solid $primary;
        padding: 1 2;
    }
    #right-panel {
        width: 1fr;
    }
    #config-info {
        height: auto;
        margin: 0 0 1 0;
        padding: 1 2;
        background: $surface;
    }
    #stages-table {
        height: auto;
        max-height: 20;
    }
    #vlm-status {
        height: 1;
        margin: 1 0 0 0;
        color: $text-muted;
    }
    #controls {
        height: auto;
        margin: 1 0;
        align: left middle;
    }
    #run-btn { margin: 0 1 0 0; }
    #summary {
        height: auto;
        margin: 1 0 0 0;
        padding: 1 2;
        background: $surface;
    }
    #log {
        height: 1fr;
        border: solid $primary;
        margin: 1 2;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "run", "Run", show=True),
    ]

    def __init__(self, settings: Settings, pdf_path: str):
        super().__init__()
        self._settings = settings
        self._pdf_path = pdf_path
        self._is_running = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="left-panel"):
                yield Static(id="config-info")
                yield DataTable(id="stages-table", cursor_type="none", zebra_stripes=True)
                yield Static("", id="vlm-status")
                with Horizontal(id="controls"):
                    yield Button("Run Pipeline", id="run-btn", variant="primary")
                yield Static("", id="summary")
            with Vertical(id="right-panel"):
                yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self._show_config()
        self._init_stages_table()

    def _show_config(self) -> None:
        s = self._settings
        info = self.query_one("#config-info", Static)
        pdf_name = Path(self._pdf_path).name
        lines = [
            f"[b]PDF:[/b]       {pdf_name}",
            f"[b]DB:[/b]        ...{s.database_url[-30:]}" if s.database_url else "[red]No DATABASE_URL[/red]",
            f"[b]VLM:[/b]       {s.vlm_model}",
            f"[b]Embed:[/b]     {s.embedding_model}" if s.embedding_api_key else "[b]Embed:[/b]     disabled",
            f"[b]Concurrency:[/b] {s.concurrency}",
        ]
        info.update("\n".join(lines))

    def _init_stages_table(self) -> None:
        table = self.query_one("#stages-table", DataTable)
        self._col_icon, self._col_stage, self._col_time, self._col_detail = (
            table.add_columns("", "Stage", "Time", "Detail")
        )
        for name in STAGE_NAMES:
            table.add_row("○", name, "", "", key=name)

    def _reset_stages(self) -> None:
        table = self.query_one("#stages-table", DataTable)
        for name in STAGE_NAMES:
            table.update_cell(name, self._col_icon, "○")
            table.update_cell(name, self._col_time, "")
            table.update_cell(name, self._col_detail, "")
        self.query_one("#summary", Static).update("")
        self.query_one("#vlm-status", Static).update("")

    # -- Actions --

    def action_run(self) -> None:
        if not self._is_running:
            self._do_run()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-btn" and not self._is_running:
            self._do_run()

    # -- Pipeline execution --

    def _log(self, text: str) -> None:
        self.call_from_thread(self.query_one("#log", RichLog).write, text)

    def _on_stage_start(self, name: str) -> None:
        table = self.query_one("#stages-table", DataTable)
        self.call_from_thread(table.update_cell, name, self._col_icon, "●")
        self.call_from_thread(table.update_cell, name, self._col_detail, "running...")
        self._log(f"\n[bold]▸ {name}[/bold]")

    def _on_stage_done(self, result: StageResult) -> None:
        table = self.query_one("#stages-table", DataTable)
        icon = "[green]✓[/green]" if result.ok else "[red]✗[/red]"
        time_str = f"{result.elapsed:.2f}s"
        detail = result.detail[:40] if result.detail else ""
        self.call_from_thread(table.update_cell, result.name, self._col_icon, icon)
        self.call_from_thread(table.update_cell, result.name, self._col_time, time_str)
        self.call_from_thread(table.update_cell, result.name, self._col_detail, detail)

    def _on_vlm_status(self, in_flight: int, completed: int, failed: int) -> None:
        parts = [f"In-flight: [bold]{in_flight}[/bold]", f"Done: [green]{completed}[/green]"]
        if failed:
            parts.append(f"Failed: [red]{failed}[/red]")
        status = self.query_one("#vlm-status", Static)
        self.call_from_thread(status.update, " | ".join(parts))

    def _on_vlm_retry(self, attempt: int, max_retries: int, msg: str, wait: float) -> None:
        self._log(f"  [yellow]Retry {attempt}/{max_retries}: {msg} — {wait:.1f}s[/yellow]")

    @work(thread=True, exclusive=True)
    def _do_run(self) -> None:
        self._is_running = True
        btn = self.query_one("#run-btn", Button)
        log = self.query_one("#log", RichLog)
        self.call_from_thread(log.clear)
        self.call_from_thread(setattr, btn, "disabled", True)
        self.call_from_thread(setattr, btn, "label", "Running...")
        self.call_from_thread(self._reset_stages)

        runner = PipelineRunner(
            settings=self._settings,
            pdf_path=self._pdf_path,
            on_log=self._log,
            on_stage_start=self._on_stage_start,
            on_stage_done=self._on_stage_done,
            on_vlm_status=self._on_vlm_status,
            on_vlm_retry=self._on_vlm_retry,
        )

        results = runner.run()

        # Summary
        ok = sum(1 for r in results if r.ok)
        total = len(results)
        total_time = sum(r.elapsed for r in results)
        failed = [r for r in results if not r.ok]

        if failed:
            summary = f"[red]{ok}/{total} passed[/red] — {total_time:.2f}s total"
            self.call_from_thread(self.notify, f"{len(failed)} stage(s) failed", severity="error")
        else:
            summary = f"[green]{ok}/{total} passed[/green] — {total_time:.2f}s total"
            self.call_from_thread(self.notify, "Pipeline complete!", severity="information")

        self.call_from_thread(self.query_one("#summary", Static).update, summary)
        self._log(f"\n[bold]{summary}[/bold]")

        self.call_from_thread(setattr, btn, "disabled", False)
        self.call_from_thread(setattr, btn, "label", "Run Pipeline")
        self._is_running = False
