import csv
import json
from pathlib import Path

from rich.console import Console

from phantombench.config import Config
from phantombench.review import RUNS_DIR, Unit, _clean_units, _injected_units, _parse_findings

SCORES_DIR = Path("data/scores")
WORKSHEET_PATH = SCORES_DIR / "worksheet.csv"
GUIDE_PATH = SCORES_DIR / "SCORING_GUIDE.md"

# finding_index 0 is a placeholder row (zero findings, or unparsable output) —
# real findings are numbered from 1 in the order the model returned them.
PLACEHOLDER_INDEX = 0

FIELDNAMES = [
    "unit_id",
    "pr_number",
    "unit_type",
    "defect_class",
    "gt_file",
    "gt_line",
    "gt_summary",
    "detection_hint",
    "model_id",
    "finding_index",
    "severity",
    "finding_file",
    "finding_line",
    "failure_mode",
    "impact",
    "suggested_fix",
    "raw_content",
    "draft_tag",
    "draft_rationale",
    "tag",
    "notes",
]

# Per §5 Stage 4. Prefilled values are deterministic facts (no findings, or
# output didn't parse) — not judgment calls — so pre-filling them isn't the
# "automatic scorer" the spec warns against. Every non-prefilled tag is
# scored by hand.
INJECTED_TAGS = {"described_catch", "localized_catch", "unrelated", "miss", "schema_violation"}
CLEAN_TAGS = {"false_positive", "true_finding", "nit", "none", "schema_violation"}

console = Console()


def _unit_id(unit: Unit) -> str:
    return f"{unit.pr_number}/{unit.defect_stem}"


def _load_existing(path: Path) -> dict[tuple[str, str, int], dict]:
    if not path.exists():
        return {}
    existing = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            key = (row["unit_id"], row["model_id"], int(row["finding_index"]))
            existing[key] = row
    return existing


def _row(unit: Unit, model_id: str, finding_index: int, prefill_tag: str, **kwargs) -> dict:
    gt = unit.ground_truth
    row = {name: "" for name in FIELDNAMES}
    row.update(
        unit_id=_unit_id(unit),
        pr_number=unit.pr_number,
        unit_type="injected" if gt is not None else "clean",
        defect_class=gt["defect_class"] if gt else "",
        gt_file=gt["file"] if gt else "",
        gt_line=gt["line_start"] if gt else "",
        gt_summary=gt["summary"].strip() if gt else "",
        detection_hint=gt["detection_hint"].strip() if gt else "",
        model_id=model_id,
        finding_index=finding_index,
        tag=prefill_tag,
    )
    row.update(kwargs)
    return row


def _rows_for_unit_model(unit: Unit, model_id: str) -> list[dict]:
    run_path = RUNS_DIR / str(unit.pr_number) / unit.defect_stem / f"{model_id}.json"
    if not run_path.exists():
        return []

    record = json.loads(run_path.read_text())
    findings = _parse_findings(record["raw_content"])
    no_findings_tag = "miss" if unit.ground_truth is not None else "none"

    if findings is None:
        return [
            _row(
                unit,
                model_id,
                PLACEHOLDER_INDEX,
                "schema_violation",
                raw_content=record["raw_content"] or "(empty — likely truncated, check finish_reason in the run file)",
            )
        ]
    if not findings:
        return [_row(unit, model_id, PLACEHOLDER_INDEX, no_findings_tag)]

    return [
        _row(
            unit,
            model_id,
            i,
            "",
            severity=f.get("severity", ""),
            finding_file=f.get("file", ""),
            finding_line=f.get("line", ""),
            failure_mode=f.get("failure_mode", ""),
            impact=f.get("impact", ""),
            suggested_fix=f.get("suggested_fix", ""),
        )
        for i, f in enumerate(findings, start=1)
    ]


def _merge(row: dict, existing: dict[tuple[str, str, int], dict]) -> dict:
    key = (row["unit_id"], row["model_id"], int(row["finding_index"]))
    prior = existing.get(key)
    if prior is None:
        return row
    # A human may have already read and scored this row — never clobber their
    # tag/notes, even if they overrode a prefilled deterministic tag (e.g. the
    # Gemini prose-wrapped-[] case is a schema violation that's also arguably
    # a `none`; the human's call wins either way).
    row["tag"] = prior.get("tag", "") or row["tag"]
    row["notes"] = prior.get("notes", "")
    # Draft columns hold machine-proposed labels awaiting human verification;
    # they are as clobber-protected as the human's own columns.
    row["draft_tag"] = prior.get("draft_tag", "")
    row["draft_rationale"] = prior.get("draft_rationale", "")
    return row


def _write_guide() -> None:
    GUIDE_PATH.write_text(
        """# Scoring guide

Fill in the `tag` column of `worksheet.csv` by hand (a spreadsheet app is
easiest — freeze the header row). `notes` is free text for anything worth
remembering later (e.g. why a call was close). Full rubric: the "How scoring works" section of the repo README.

Re-running `phantombench score` regenerates the worksheet from
`data/runs/` but always preserves any `tag`/`notes` you've already filled
in — safe to re-run after a fresh review run adds units.

## Injected units (`unit_type=injected`) — one row per model finding

- `described_catch` — the finding names the actual failure, per `detection_hint`.
- `localized_catch` — lands on/near the injected line(s) but describes a
  different (wrong) failure.
- `unrelated` — a finding elsewhere in the diff, not about the injected defect.
- `miss` — prefilled when the model returned zero findings. Nothing to score.

A unit/model's catch rate is the *best* tag among its findings
(described > localized > miss) — a stray `unrelated` finding elsewhere in the
same response doesn't cancel out a real catch, and is worth keeping visible
as its own false-alarm data point rather than discarding.

## Clean units (`unit_type=clean`) — one row per model finding

- `false_positive` — a blocking or should-fix comment on code with no known defect.
- `true_finding` — the comment identifies a real, genuine pre-existing bug in
  the merged code. Not a false positive — note it, it's talk-worthy.
- `nit` — recorded but excluded from the false-positive count.
- `none` — prefilled when the model returned zero findings.

## Any unit — `schema_violation`

Prefilled when the model's raw output didn't parse as a JSON findings array
(truncation, prose wrapping the JSON, etc.). Read `raw_content` by hand — if
a real finding is legible inside it, override the tag to whatever it would
have scored as and say so in `notes`; the schema violation itself is still
worth keeping in `notes` since it's a rubric-compliance data point.
"""
    )


def run(config: Config) -> None:
    units = sorted(_injected_units() + _clean_units(), key=lambda u: (u.pr_number, u.defect_stem))
    if not units:
        raise SystemExit("No units found. Run `phantombench scrape`, `inject`, and `review` first.")

    existing = _load_existing(WORKSHEET_PATH)

    rows = []
    missing_runs = []
    for unit in units:
        for model in config.models:
            unit_rows = _rows_for_unit_model(unit, model.id)
            if not unit_rows:
                missing_runs.append(f"{_unit_id(unit)}/{model.id}")
                continue
            rows.extend(_merge(row, existing) for row in unit_rows)

    SCORES_DIR.mkdir(parents=True, exist_ok=True)
    with WORKSHEET_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    _write_guide()

    scored = sum(1 for r in rows if r["tag"] and r["tag"] not in {"miss", "none", "schema_violation"})
    prefilled = sum(1 for r in rows if r["tag"] in {"miss", "none", "schema_violation"})
    unscored = len(rows) - scored - prefilled

    console.print(f"[green]wrote[/] {WORKSHEET_PATH} — {len(rows)} rows across {len(units)} units")
    console.print(f"  {scored} hand-scored, {prefilled} prefilled (deterministic), {unscored} awaiting a tag")
    console.print(f"[dim]scoring guide: {GUIDE_PATH}[/]")
    if missing_runs:
        console.print(
            f"[yellow]{len(missing_runs)} unit/model pairs have no run on disk yet[/] "
            "(run `phantombench review` first): " + ", ".join(missing_runs)
        )
