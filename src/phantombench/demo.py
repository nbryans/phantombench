"""The live-talk demo (§7).

Runs a curated three-unit subset across every configured model, all calls in
parallel, streaming a terminal table as results land. `--replay` serves the
same table from `data/runs/` at the recorded pacing with zero network calls.

Deliberately does not judge anything. Every number in the table is a mechanical
fact (how long, how many findings, at what severity, on which lines); the
catch-vs-false-alarm call is the hand-scoring pass's job, and saying so on
stage is the point rather than a caveat.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from phantombench.config import Config, ModelConfig
from phantombench.review import (
    RUNS_DIR,
    RunError,
    Unit,
    _build_prompt,
    _call_openrouter,
    _parse_findings,
    _persist_run,
    _require_openrouter_key,
    _resolve_units,
)

DEMO_RUNS_DIR = Path("data/demo_runs")
DEFAULT_UNITS = ["1628/008-conditional-span-end", "1628/clean", "1811/001-exclude-none"]
DEFAULT_TIMEOUT_SECONDS = 75.0
DEFAULT_REPLAY_SPEED = 1.0
NOISE_SEVERITIES = ("blocking", "should-fix")

console = Console()


class DemoError(Exception):
    """Anything that should end the demo with a legible message, not a traceback."""


@dataclass
class Cell:
    """One (unit, model) square of the grid."""

    unit_index: int
    model: ModelConfig
    state: str = "waiting"  # waiting | calling | done | failed
    latency: float | None = None
    findings: list[dict] | None = None
    finish_reason: str | None = None
    error: str | None = None
    started_at: float | None = None

    @property
    def unparsable(self) -> bool:
        return self.state == "done" and self.findings is None


@dataclass
class DemoUnit:
    unit: Unit
    cells: list[Cell] = field(default_factory=list)


def _shorten(text: str, limit: int) -> str:
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rsplit(" ", 1)[0] + "…"


def _severity_counts(findings: list[dict]) -> dict[str, int]:
    counts = {"blocking": 0, "should-fix": 0, "nit": 0}
    for finding in findings:
        severity = str(finding.get("severity", "")).strip().lower()
        if severity in counts:
            counts[severity] += 1
    return counts


def _flagged_at(findings: list[dict]) -> str:
    locations = []
    for finding in findings:
        name = str(finding.get("file", "?")).rsplit("/", 1)[-1]
        locations.append(f"{name}:{finding.get('line', '?')}")
    return ", ".join(locations)


# --- resolution ------------------------------------------------------------


def _demo_settings(config: Config) -> tuple[list[str], float, float]:
    demo = config.demo or {}
    unit_ids = demo.get("units") or DEFAULT_UNITS
    if not isinstance(unit_ids, list) or not all(isinstance(u, str) for u in unit_ids):
        raise DemoError("config.yaml `demo.units` must be a list of unit ids, e.g. ['1811/clean'].")
    return (
        unit_ids,
        float(demo.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        float(demo.get("replay_speed", DEFAULT_REPLAY_SPEED)),
    )


def _resolve_demo_units(unit_ids: list[str]) -> list[Unit]:
    units = []
    for unit_id in unit_ids:
        try:
            units.extend(_resolve_units(unit_id))
        except SystemExit as exc:
            raise DemoError(f"config.yaml `demo.units` entry {unit_id!r} did not resolve.\n{exc}") from exc
    return units


def _cached_path(unit: Unit, model: ModelConfig) -> Path:
    return RUNS_DIR / str(unit.pr_number) / unit.defect_stem / f"{model.id}.json"


def _preflight(demo_units: list[DemoUnit], models: list[ModelConfig], replay: bool) -> str | None:
    """Fail before anything is on screen, not halfway through the demo."""
    if not replay:
        _require_openrouter_key()  # raises SystemExit with its own legible message
        return None

    missing = [
        str(_cached_path(du.unit, model))
        for du in demo_units
        for model in models
        if not _cached_path(du.unit, model).exists()
    ]
    if missing:
        raise DemoError(
            "--replay needs a cached response for every demo unit/model pair, and these are missing:\n  "
            + "\n  ".join(missing)
            + "\n\nRun `phantombench review` first, or point `demo.units` in config.yaml at units you have."
        )
    return None


# --- workers ---------------------------------------------------------------


def _record_response(cell: Cell, record: dict) -> None:
    cell.findings = _parse_findings(record.get("raw_content"))
    choices = (record.get("raw_response") or {}).get("choices") or [{}]
    cell.finish_reason = choices[0].get("finish_reason")
    cell.state = "done"


def _work_live(
    cell: Cell,
    unit: Unit,
    api_key: str,
    review_cfg: dict,
    timeout: float,
    out_dir: Path,
) -> None:
    cell.started_at = time.monotonic()
    cell.state = "calling"
    try:
        # No retry budget: a demo cannot afford the full run's exponential
        # backoff, and a failed cell is honest talk material anyway.
        response, latency = _call_openrouter(
            api_key, cell.model, _build_prompt(unit.diff), review_cfg, max_retries=0, timeout=timeout
        )
        path = _persist_run(unit, cell.model, response, latency, runs_dir=out_dir)
        record = json.loads(path.read_text())
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — a stack trace must never be what's on screen
        cell.state = "failed"
        cell.latency = time.monotonic() - cell.started_at
        cell.error = _shorten(str(exc) or exc.__class__.__name__, 90)
        return
    cell.latency = latency
    _record_response(cell, record)


def _work_replay(cell: Cell, unit: Unit, speed: float) -> None:
    cell.started_at = time.monotonic()
    cell.state = "calling"
    try:
        record = json.loads(_cached_path(unit, cell.model).read_text())
        recorded = float(record["latency_seconds"])
        time.sleep(recorded / speed)
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        cell.state = "failed"
        cell.latency = time.monotonic() - cell.started_at
        cell.error = _shorten(str(exc) or exc.__class__.__name__, 90)
        return
    cell.latency = recorded
    _record_response(cell, record)


# --- rendering -------------------------------------------------------------


def _unit_caption(unit: Unit) -> Text:
    if unit.ground_truth is None:
        return Text.assemble(
            ("planted: nothing. ", "bold"),
            ("Every blocking or should-fix comment below is a comment on code that shipped as-is.", "dim"),
        )
    gt = unit.ground_truth
    return Text.assemble(
        ("planted: ", "bold"),
        (f"{gt['file'].rsplit('/', 1)[-1]}:{gt['line_start']} — {_shorten(gt['summary'], 120)}", "dim"),
    )


def _unit_title(unit: Unit, index: int, total: int) -> Text:
    if unit.ground_truth is None:
        kind = Text(" CLEAN CONTROL ", style="bold black on yellow")
        detail = Text(f"  PR #{unit.pr_number}, unmodified — the same PR as the injected unit above")
    else:
        kind = Text(" INJECTED ", style="bold white on red")
        detail = Text(f"  PR #{unit.pr_number} · {unit.ground_truth['defect_class']} · {unit.defect_stem}")
    return Text.assemble(f"[{index}/{total}] ", kind, detail)


def _cell_row(cell: Cell, now: float) -> list[Text]:
    model = Text(cell.model.id, style="bold")

    if cell.state in ("waiting", "calling"):
        elapsed = f"{now - cell.started_at:4.1f}s" if cell.started_at else "  · "
        return [model, Text(elapsed, style="dim"), Text("", style="dim"), Text(""), Text(""), Text("calling…", style="dim")]

    time_cell = Text(f"{cell.latency:4.1f}s" if cell.latency is not None else "—")

    if cell.state == "failed":
        return [model, time_cell, Text("—"), Text(""), Text(""), Text(f"call failed: {cell.error}", style="bold red")]

    if cell.unparsable:
        reason = f"unparsable (finish: {cell.finish_reason})"
        return [model, time_cell, Text("—"), Text(""), Text(""), Text(reason, style="bold red")]

    findings = cell.findings or []
    counts = _severity_counts(findings)
    if not findings:
        return [model, time_cell, Text("0", style="bold green"), Text("·", style="dim"), Text("·", style="dim"), Text("no findings", style="green")]

    return [
        model,
        time_cell,
        Text(str(len(findings)), style="bold"),
        Text(str(counts["blocking"]) if counts["blocking"] else "·", style="bold red" if counts["blocking"] else "dim"),
        Text(str(counts["should-fix"]) if counts["should-fix"] else "·", style="yellow" if counts["should-fix"] else "dim"),
        Text(_flagged_at(findings)),
    ]


def _render(demo_units: list[DemoUnit], now: float) -> Group:
    blocks: list = []
    for index, du in enumerate(demo_units, start=1):
        # expand=True so all three grids come out the same width — ragged table
        # edges read as sloppy on a projector.
        table = Table(box=box.SIMPLE_HEAD, padding=(0, 1), expand=True, pad_edge=False)
        table.add_column("model", width=8)
        table.add_column("time", width=6, justify="right")
        table.add_column("found", width=5, justify="right")
        table.add_column("blk", width=3, justify="right")
        table.add_column("s-fix", width=5, justify="right")
        table.add_column("flagged at", ratio=1, overflow="ellipsis", no_wrap=True)
        for cell in du.cells:
            table.add_row(*_cell_row(cell, now))
        blocks.extend([Text(""), _unit_title(du.unit, index, len(demo_units)), _unit_caption(du.unit), table])
    return Group(*blocks)


def _print_transcript(demo_units: list[DemoUnit]) -> None:
    console.print()
    console.rule("[bold]what they actually said")
    for index, du in enumerate(demo_units, start=1):
        console.print()
        console.print(_unit_title(du.unit, index, len(demo_units)))

        # A table rather than plain prints: failure_mode text wraps inside its
        # own cell, so long findings stay aligned instead of running back to
        # column zero — which is unreadable at projector font size.
        table = Table(box=None, padding=(0, 1), expand=True, pad_edge=False, show_header=False)
        table.add_column(width=7)
        table.add_column(width=23)
        table.add_column(ratio=1)

        for cell in du.cells:
            model = Text(cell.model.id, style="bold")
            if cell.state == "failed":
                table.add_row(model, Text("call failed", style="bold red"), Text(cell.error or "", style="red"))
                continue
            if cell.unparsable:
                table.add_row(
                    model,
                    Text("unparsable", style="bold red"),
                    Text(f"output was not a JSON findings array (finish_reason: {cell.finish_reason})", style="red"),
                )
                continue
            if not cell.findings:
                table.add_row(model, Text("no findings", style="green"), Text(""))
                continue
            for position, finding in enumerate(cell.findings):
                severity = str(finding.get("severity", "?")).lower()
                style = {"blocking": "bold red", "should-fix": "yellow"}.get(severity, "dim")
                name = str(finding.get("file", "?")).rsplit("/", 1)[-1]
                table.add_row(
                    model if position == 0 else Text(""),
                    Text.assemble((severity, style), " ", (f"{name}:{finding.get('line', '?')}", "dim")),
                    Text(_shorten(finding.get("failure_mode", ""), 220)),
                )
        console.print(table)


def _print_summary(demo_units: list[DemoUnit], models: list[ModelConfig], wall: float, replay: bool) -> None:
    console.print()
    table = Table(box=box.SIMPLE_HEAD, padding=(0, 1), expand=False, pad_edge=False, title=None)
    table.add_column("model", width=8)
    table.add_column("on the injected units", width=36, no_wrap=True)
    table.add_column("on the clean control", width=36, no_wrap=True)

    for model in models:
        injected, clean = [], []
        for du in demo_units:
            cell = next(c for c in du.cells if c.model.id == model.id)
            noisy = sum(_severity_counts(cell.findings or []).get(s, 0) for s in NOISE_SEVERITIES)
            (injected if du.unit.ground_truth is not None else clean).append((cell, noisy))

        def summarize(cells: list[tuple[Cell, int]]) -> Text:
            if not cells:
                return Text("—", style="dim")
            total = sum(n for _, n in cells)
            broken = sum(1 for c, _ in cells if c.state == "failed" or c.unparsable)
            label = f"{total} blocking/should-fix"
            if broken:
                label += f", {broken} unparsable"
            style = "green" if total == 0 and not broken else ""
            return Text(label, style=style)

        table.add_row(Text(model.id, style="bold"), summarize(injected), summarize(clean))

    console.print(table)
    source = "replayed from data/runs/" if replay else "live model calls"
    console.print(
        f"[dim]{len(demo_units) * len(models)} reviews · {source} · {wall:.1f}s wall clock. "
        f"Catch vs. false alarm is scored by hand — see data/scores/.[/]"
    )


# --- entrypoint ------------------------------------------------------------


def run(config: Config, replay: bool = False, speed: float | None = None) -> None:
    unit_ids, timeout, configured_speed = _demo_settings(config)
    speed = configured_speed if speed is None else speed
    if speed <= 0:
        raise DemoError("--speed must be greater than 0.")

    models = config.models
    if not models:
        raise DemoError("No models configured in config.yaml.")

    demo_units = [DemoUnit(unit=u) for u in _resolve_demo_units(unit_ids)]
    for index, du in enumerate(demo_units):
        du.cells = [Cell(unit_index=index, model=m) for m in models]
    cells = [(du, cell) for du in demo_units for cell in du.cells]

    _preflight(demo_units, models, replay)

    api_key = None if replay else _require_openrouter_key()
    out_dir = DEMO_RUNS_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    console.clear()
    console.print()
    console.print(
        Panel(
            Text.assemble(
                ("Who Reviews the Reviewer?", "bold"),
                (f"  ·  {len(demo_units)} diffs × {len(models)} models, in parallel\n", ""),
                (
                    "Replaying recorded responses — no network calls."
                    if replay
                    else "Calling all three models live.",
                    "dim",
                ),
            ),
            box=box.HEAVY,
            expand=False,
            padding=(0, 2),
        )
    )

    started = time.monotonic()
    with Live(_render(demo_units, started), console=console, refresh_per_second=4) as live:
        with ThreadPoolExecutor(max_workers=len(cells)) as pool:
            futures = [
                pool.submit(_work_replay, cell, du.unit, speed)
                if replay
                else pool.submit(_work_live, cell, du.unit, api_key, config.review, timeout, out_dir)
                for du, cell in cells
            ]
            while not all(f.done() for f in futures):
                live.update(_render(demo_units, time.monotonic()))
                time.sleep(0.25)
        live.update(_render(demo_units, time.monotonic()))
    wall = time.monotonic() - started

    _print_transcript(demo_units)
    _print_summary(demo_units, models, wall, replay)

    if not replay:
        console.print(f"[dim]Raw responses written to {out_dir}/ (data/runs/ is left untouched).[/]")

    failed = [f"{du.unit.pr_number}/{du.unit.defect_stem}/{cell.model.id}" for du, cell in cells if cell.state == "failed"]
    if failed:
        raise DemoError("These calls failed: " + ", ".join(failed))
