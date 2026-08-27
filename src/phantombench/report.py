"""Stage 5 — turn the scored worksheet into tables and the two talk charts (§5).

Reads `tag` from `data/scores/worksheet.csv`, falling back to `draft_tag` for
any row a human hasn't verified yet. Every output is stamped with which of the
two it used: a chart built partly from drafts says so on its own face, so a
provisional number can never be mistaken for a verified one on a projector.
When the `tag` column is complete the stamp disappears on its own.
"""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no display on a build box; must precede pyplot
import matplotlib.pyplot as plt  # noqa: E402

from rich import box  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from phantombench.config import Config  # noqa: E402
from phantombench.review import RUNS_DIR  # noqa: E402
from phantombench.score import WORKSHEET_PATH  # noqa: E402

REPORTS_DIR = Path("reports")
SUMMARY_PATH = REPORTS_DIR / "summary.md"
CATCH_VS_FP_PATH = REPORTS_DIR / "catch_vs_fp.png"
OVERLAP_PATH = REPORTS_DIR / "overlap.png"

# Best-tag ranking for an injected unit: a stray `unrelated` finding elsewhere
# in the same response never cancels out a real catch (SCORING_GUIDE.md).
CATCH_RANK = {"described_catch": 3, "localized_catch": 2, "unrelated": 1, "miss": 0, "schema_violation": 0}
CAUGHT = "described_catch"
LOCALIZED = "localized_catch"

# Talk palette: high contrast, distinguishable in grayscale and to the most
# common colour-vision deficiencies.
MODEL_COLORS = ["#1b6ca8", "#d1495b", "#e6a700"]
OVERLAP_COLORS = {3: "#17603a", 2: "#4c9f70", 1: "#e6a700", 0: "#d1495b"}

console = Console()


class ReportError(Exception):
    """Something the user can fix, reported without a traceback."""


@dataclass
class Totals:
    """Everything one model scored, across both halves of the paired design."""

    model_id: str
    injected_best: dict[str, str] = field(default_factory=dict)  # unit_id -> best tag
    injected_class: dict[str, str] = field(default_factory=dict)  # unit_id -> defect class
    unrelated_findings: int = 0
    false_positives: int = 0
    true_findings: int = 0
    nits: int = 0
    schema_violations: int = 0
    clean_prs_with_fp: set[str] = field(default_factory=set)

    @property
    def n_injected(self) -> int:
        return len(self.injected_best)

    @property
    def described(self) -> int:
        return sum(1 for t in self.injected_best.values() if t == CAUGHT)

    @property
    def localized_or_better(self) -> int:
        return sum(1 for t in self.injected_best.values() if t in (CAUGHT, LOCALIZED))

    def rate(self, n: int) -> float:
        return 100.0 * n / self.n_injected if self.n_injected else 0.0


def _pct(value: float) -> str:
    return f"{value:.0f}%"


# --- loading ---------------------------------------------------------------


def _effective_tag(row: dict) -> tuple[str, str]:
    """Return (tag, provenance). Human `tag` always wins over `draft_tag`."""
    if row.get("tag", "").strip():
        return row["tag"].strip(), "verified"
    if row.get("draft_tag", "").strip():
        return row["draft_tag"].strip(), "draft"
    return "", "untagged"


def _load_rows() -> list[dict]:
    if not WORKSHEET_PATH.exists():
        raise ReportError(f"No worksheet at {WORKSHEET_PATH}. Run `phantombench score` first.")
    with WORKSHEET_PATH.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ReportError(f"{WORKSHEET_PATH} has no rows. Run `phantombench review` then `phantombench score`.")
    return rows


def _aggregate(rows: list[dict], model_ids: list[str]) -> tuple[dict[str, Totals], Counter, list[str], set[str]]:
    totals = {m: Totals(model_id=m) for m in model_ids}
    provenance: Counter = Counter()
    untagged: list[str] = []
    clean_prs: set[str] = set()

    for row in rows:
        model_id = row["model_id"]
        if model_id not in totals:
            continue
        tag, source = _effective_tag(row)
        provenance[source] += 1
        t = totals[model_id]
        unit_id = row["unit_id"]

        if not tag:
            untagged.append(f"{unit_id}/{model_id}#{row['finding_index']}")
            continue

        if row["unit_type"] == "injected":
            t.injected_class[unit_id] = row["defect_class"]
            prior = t.injected_best.get(unit_id, "miss")
            if CATCH_RANK.get(tag, 0) > CATCH_RANK.get(prior, 0):
                t.injected_best[unit_id] = tag
            elif unit_id not in t.injected_best:
                t.injected_best[unit_id] = tag
            if tag == "unrelated":
                t.unrelated_findings += 1
            if tag == "schema_violation":
                t.schema_violations += 1
        else:
            clean_prs.add(unit_id)
            if tag == "false_positive":
                t.false_positives += 1
                t.clean_prs_with_fp.add(unit_id)
            elif tag == "true_finding":
                t.true_findings += 1
            elif tag == "nit":
                t.nits += 1
            elif tag == "schema_violation":
                t.schema_violations += 1

    return totals, provenance, untagged, clean_prs


def _run_costs(model_ids: list[str]) -> dict[str, dict]:
    """Cost and latency come from the raw runs, not the worksheet — OpenRouter
    reports real per-call cost in usage.cost."""
    stats = {m: {"calls": 0, "cost": 0.0, "latencies": [], "prompt": 0, "completion": 0, "reasoning": 0} for m in model_ids}
    for path in RUNS_DIR.glob("*/*/*.json"):
        model_id = path.stem
        if model_id not in stats:
            continue
        record = json.loads(path.read_text())
        usage = (record.get("raw_response") or {}).get("usage") or {}
        s = stats[model_id]
        s["calls"] += 1
        s["cost"] += float(usage.get("cost") or 0.0)
        s["latencies"].append(float(record.get("latency_seconds") or 0.0))
        s["prompt"] += int(record.get("prompt_tokens") or 0)
        s["completion"] += int(record.get("completion_tokens") or 0)
        s["reasoning"] += int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
    return stats


def _overlap_by_class(totals: dict[str, Totals]) -> dict[str, Counter]:
    """For each injected unit, how many models produced a described_catch."""
    caught_by: dict[str, int] = defaultdict(int)
    unit_class: dict[str, str] = {}
    for t in totals.values():
        for unit_id, tag in t.injected_best.items():
            unit_class[unit_id] = t.injected_class[unit_id]
            caught_by[unit_id] += 1 if tag == CAUGHT else 0
    by_class: dict[str, Counter] = defaultdict(Counter)
    for unit_id, defect_class in unit_class.items():
        by_class[defect_class][caught_by[unit_id]] += 1
    return by_class


# --- charts ----------------------------------------------------------------


def _apply_talk_style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "font.size": 15,
            "axes.titlesize": 21,
            "axes.labelsize": 17,
            "xtick.labelsize": 15,
            "ytick.labelsize": 15,
            "legend.fontsize": 14,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.alpha": 0.25,
        }
    )


def _stamp(fig, provisional: bool) -> None:
    if not provisional:
        return
    fig.text(
        0.5,
        0.5,
        "PROVISIONAL",
        fontsize=64,
        color="#d1495b",
        alpha=0.13,
        ha="center",
        va="center",
        rotation=24,
        weight="bold",
        zorder=10,
    )


def _chart_catch_vs_fp(totals: dict[str, Totals], n_clean: int, provisional: bool, subtitle: str) -> None:
    _apply_talk_style()
    fig, ax = plt.subplots(figsize=(11, 7.5))

    for i, t in enumerate(totals.values()):
        x = t.rate(t.described)
        y = t.false_positives / n_clean if n_clean else 0.0
        color = MODEL_COLORS[i % len(MODEL_COLORS)]
        ax.scatter(x, y, s=520, color=color, edgecolor="white", linewidth=2.5, zorder=5)
        # Labels sit to the left of their marker: catch rates cluster near 100%,
        # so anything centred or to the right either collides or pushes the
        # x-axis past 100% — which reads as an error on a slide about rigour.
        ax.annotate(
            f"{t.model_id}\n{t.described}/{t.n_injected} caught · {t.false_positives} FPs",
            (x, y),
            textcoords="offset points",
            xytext=(-26, 0),
            ha="right",
            va="center",
            fontsize=15,
            color=color,
            weight="bold",
        )

    ax.set_xlabel("defects caught and correctly described  (%)")
    ax.set_ylabel(f"false positives per clean PR  (n={n_clean})")
    ax.set_title("Catch rate alone tells you nothing", pad=34)
    ax.set_xlim(0, 104)
    ax.set_xticks(range(0, 101, 20))
    fp_values = [t.false_positives / n_clean for t in totals.values()] if n_clean else [0]
    ax.set_ylim(0, max(fp_values) * 1.45 + 0.3)
    ax.text(
        0.5,
        1.035,
        subtitle,
        transform=ax.transAxes,
        ha="center",
        fontsize=14,
        color="#555555",
    )
    _stamp(fig, provisional)
    fig.tight_layout()
    fig.savefig(CATCH_VS_FP_PATH, dpi=150)
    plt.close(fig)


def _chart_overlap(by_class: dict[str, Counter], n_models: int, provisional: bool, subtitle: str) -> None:
    _apply_talk_style()
    fig, ax = plt.subplots(figsize=(11, 7.5))

    classes = sorted(by_class, key=lambda c: -sum(by_class[c].values()))
    bottoms = [0.0] * len(classes)
    for bucket in range(n_models, -1, -1):
        heights = [by_class[c].get(bucket, 0) for c in classes]
        if not any(heights):
            continue
        ax.bar(
            classes,
            heights,
            bottom=bottoms,
            color=OVERLAP_COLORS.get(bucket, "#999999"),
            edgecolor="white",
            linewidth=1.5,
            label=f"caught by {bucket}" if bucket else "caught by none",
        )
        for i, h in enumerate(heights):
            if h:
                ax.text(i, bottoms[i] + h / 2, str(h), ha="center", va="center", fontsize=15, color="white", weight="bold")
        bottoms = [b + h for b, h in zip(bottoms, heights)]

    ax.set_ylabel("injected defects")
    caught_by_all = sum(counts.get(n_models, 0) for counts in by_class.values())
    total_defects = sum(sum(counts.values()) for counts in by_class.values())
    ax.set_title(f"{caught_by_all} of {total_defects} defects were caught by all three models", pad=34)
    ax.set_yticks(range(0, int(max(bottoms)) + 1))
    ax.tick_params(axis="x", rotation=18)
    ax.legend(frameon=False, loc="upper right")
    ax.text(0.5, 1.035, subtitle, transform=ax.transAxes, ha="center", fontsize=14, color="#555555")
    _stamp(fig, provisional)
    fig.tight_layout()
    fig.savefig(OVERLAP_PATH, dpi=150)
    plt.close(fig)


# --- markdown --------------------------------------------------------------


def _summary_markdown(
    totals: dict[str, Totals],
    by_class: dict[str, Counter],
    costs: dict[str, dict],
    n_clean: int,
    provenance: Counter,
    provisional: bool,
) -> str:
    models = list(totals.values())
    lines: list[str] = ["# Who Reviews the Reviewer? — results", ""]

    if provisional:
        lines += [
            "> **PROVISIONAL.** "
            f"{provenance['draft']} of {sum(provenance.values())} findings still carry a model-drafted "
            "`draft_tag` awaiting human verification. Numbers below will move if verification "
            "overrides any draft.",
            "",
        ]
    else:
        lines += [f"All {provenance['verified']} findings carry a human-verified `tag`.", ""]

    lines += [
        f"Paired design: {models[0].n_injected} injected defects and the same "
        f"{n_clean} PRs unmodified, reviewed by {len(models)} models from the diff alone.",
        "",
        "## Catch rate per model",
        "",
        "| model | described | localized-or-better | missed | `unrelated` findings on injected PRs |",
        "|---|---|---|---|---|",
    ]
    for t in models:
        lines.append(
            f"| {t.model_id} | {t.described}/{t.n_injected} ({_pct(t.rate(t.described))}) | "
            f"{t.localized_or_better}/{t.n_injected} ({_pct(t.rate(t.localized_or_better))}) | "
            f"{t.n_injected - t.localized_or_better} | {t.unrelated_findings} |"
        )

    lines += [
        "",
        "## Catch rate per defect class",
        "",
        "| defect class | " + " | ".join(t.model_id for t in models) + " |",
        "|---" * (len(models) + 1) + "|",
    ]
    all_classes = sorted({c for t in models for c in t.injected_class.values()})
    for defect_class in all_classes:
        cells = []
        for t in models:
            units = [u for u, c in t.injected_class.items() if c == defect_class]
            caught = sum(1 for u in units if t.injected_best.get(u) == CAUGHT)
            cells.append(f"{caught}/{len(units)}")
        lines.append(f"| `{defect_class}` | " + " | ".join(cells) + " |")

    lines += [
        "",
        f"## False positives on the {n_clean} clean control PRs",
        "",
        "| model | false positives | per clean PR | clean PRs with ≥1 FP | true findings | nits | schema violations |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in models:
        per_pr = t.false_positives / n_clean if n_clean else 0
        lines.append(
            f"| {t.model_id} | {t.false_positives} | {per_pr:.2f} | "
            f"{len(t.clean_prs_with_fp)}/{n_clean} | {t.true_findings} | {t.nits} | {t.schema_violations} |"
        )

    lines += [
        "",
        "Clean control PRs are real merged code and can contain genuine pre-existing bugs; those "
        "are tagged `true_finding` and excluded from the false-positive count.",
        "",
        "## Overlap — how many models described each defect",
        "",
        "| defect class | caught by 3 | by 2 | by 1 | by none |",
        "|---|---|---|---|---|",
    ]
    totals_bucket: Counter = Counter()
    for defect_class in sorted(by_class):
        counts = by_class[defect_class]
        totals_bucket.update(counts)
        lines.append(
            f"| `{defect_class}` | {counts.get(3, 0)} | {counts.get(2, 0)} | {counts.get(1, 0)} | {counts.get(0, 0)} |"
        )
    lines.append(
        f"| **all** | **{totals_bucket.get(3, 0)}** | **{totals_bucket.get(2, 0)}** | "
        f"**{totals_bucket.get(1, 0)}** | **{totals_bucket.get(0, 0)}** |"
    )

    lines += [
        "",
        "## Cost and latency",
        "",
        "| model | calls | total cost | median latency | slowest | prompt tokens | completion tokens | of which reasoning |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in models:
        s = costs[t.model_id]
        latencies = s["latencies"] or [0.0]
        lines.append(
            f"| {t.model_id} | {s['calls']} | ${s['cost']:.2f} | {statistics.median(latencies):.1f}s | "
            f"{max(latencies):.1f}s | {s['prompt']:,} | {s['completion']:,} | {s['reasoning']:,} |"
        )
    grand = sum(s["cost"] for s in costs.values())
    lines += ["", f"Total spend across the full matrix: **${grand:.2f}**.", ""]
    lines += [
        "## Charts",
        "",
        f"- `{CATCH_VS_FP_PATH.name}` — catch rate vs. false positives per clean PR",
        f"- `{OVERLAP_PATH.name}` — overlap by defect class",
        "",
    ]
    return "\n".join(lines)


# --- terminal --------------------------------------------------------------


def _print_tables(totals: dict[str, Totals], costs: dict[str, dict], by_class: dict[str, Counter], n_clean: int) -> None:
    models = list(totals.values())

    table = Table(title="catch rate vs. false positives", box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("model", style="bold")
    table.add_column("described", justify="right")
    table.add_column("loc-or-better", justify="right")
    table.add_column("FPs", justify="right")
    table.add_column("FPs/clean PR", justify="right")
    table.add_column("clean PRs hit", justify="right")
    table.add_column("true finds", justify="right")
    for t in models:
        table.add_row(
            t.model_id,
            f"{t.described}/{t.n_injected}",
            f"{t.localized_or_better}/{t.n_injected}",
            str(t.false_positives),
            f"{t.false_positives / n_clean:.2f}" if n_clean else "—",
            f"{len(t.clean_prs_with_fp)}/{n_clean}",
            str(t.true_findings),
        )
    console.print(table)

    overlap = Counter()
    for counts in by_class.values():
        overlap.update(counts)
    console.print(
        f"[bold]overlap[/] — caught by 3: {overlap.get(3, 0)} · by 2: {overlap.get(2, 0)} · "
        f"by 1: {overlap.get(1, 0)} · by none: {overlap.get(0, 0)}"
    )
    spend = sum(s["cost"] for s in costs.values())
    console.print(f"[bold]spend[/] — ${spend:.2f} across {sum(s['calls'] for s in costs.values())} calls")


# --- entrypoint ------------------------------------------------------------


def run(config: Config) -> None:
    model_ids = [m.id for m in config.models]
    rows = _load_rows()
    totals, provenance, untagged, clean_prs = _aggregate(rows, model_ids)
    n_clean = len(clean_prs)

    if not any(t.n_injected for t in totals.values()):
        raise ReportError(
            "No tagged findings on injected units. Fill in the `tag` column of "
            f"{WORKSHEET_PATH} (see data/scores/SCORING_GUIDE.md) and re-run."
        )

    provisional = provenance["draft"] > 0
    costs = _run_costs(model_ids)
    by_class = _overlap_by_class(totals)

    total_tagged = provenance["verified"] + provenance["draft"]
    subtitle = (
        f"{provenance['verified']}/{total_tagged} findings human-verified, "
        f"{provenance['draft']} model-drafted"
        if provisional
        else f"all {provenance['verified']} findings human-verified"
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    _chart_catch_vs_fp(totals, n_clean, provisional, subtitle)
    _chart_overlap(by_class, len(model_ids), provisional, subtitle)
    SUMMARY_PATH.write_text(_summary_markdown(totals, by_class, costs, n_clean, provenance, provisional))

    console.print()
    _print_tables(totals, costs, by_class, n_clean)
    console.print()
    console.print(f"[green]wrote[/] {SUMMARY_PATH}, {CATCH_VS_FP_PATH}, {OVERLAP_PATH}")

    if provisional:
        console.print(
            f"[bold yellow]PROVISIONAL[/] — {provenance['draft']}/{total_tagged} findings are model-drafted "
            "(`draft_tag`), not yet human-verified. Charts are watermarked accordingly."
        )
    if untagged:
        console.print(
            f"[yellow]{len(untagged)} findings have neither a tag nor a draft[/] and were excluded: "
            + ", ".join(untagged[:5])
            + ("…" if len(untagged) > 5 else "")
        )
