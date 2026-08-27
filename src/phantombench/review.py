from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from rich.console import Console

from phantombench.config import Config, ModelConfig

OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"
PRS_DIR = Path("data/prs")
RUNS_DIR = Path("data/runs")
RUBRIC_PATH = Path("prompts/review_rubric.md")
ENV_PATH = Path(".env")

# Transient OpenRouter/provider failures worth retrying; anything else (auth,
# bad request, etc.) fails the whole run immediately since retrying won't help.
TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 2.0

console = Console()


@dataclass
class Unit:
    pr_number: int
    defect_stem: str  # e.g. "001-exclude-none", or "clean" for a clean-control unit
    diff: str
    ground_truth: dict | None  # None for a clean unit — no injected defect to describe
    out_dir: Path


class RunError(Exception):
    """A unit/model call failed after exhausting retries."""


def _load_dotenv(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _require_openrouter_key() -> str:
    _load_dotenv()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit(
            "OPENROUTER_API_KEY is not set. Copy .env.example to .env and fill it in "
            "(https://openrouter.ai/keys)."
        )
    return key


def _injected_units() -> list[Unit]:
    units = []
    for diff_path in sorted(PRS_DIR.glob("*/injected/*/diff.patch")):
        out_dir = diff_path.parent
        ground_truth = yaml.safe_load((out_dir / "ground_truth.yaml").read_text())
        units.append(
            Unit(
                pr_number=int(diff_path.parents[2].name),
                defect_stem=out_dir.name,
                diff=diff_path.read_text(),
                ground_truth=ground_truth,
                out_dir=out_dir,
            )
        )
    return units


def _clean_units() -> list[Unit]:
    # Clean-control half of the paired design (§2): every scraped PR,
    # unmodified. A clean unit's diff already exists on disk from `scrape` —
    # it just has no ground truth. Not gated on an injected/ subdirectory, so
    # `phantombench review` works as a zero-defect-authoring FP-rate check on
    # any repo you've pointed `scrape` at, before you've injected anything.
    units = []
    for diff_path in sorted(PRS_DIR.glob("*/diff.patch")):
        pr_dir = diff_path.parent
        units.append(
            Unit(
                pr_number=int(pr_dir.name),
                defect_stem="clean",
                diff=diff_path.read_text(),
                ground_truth=None,
                out_dir=pr_dir,
            )
        )
    return units


def _resolve_units(unit_id: str | None) -> list[Unit]:
    units = _injected_units() + _clean_units()
    if not units:
        raise SystemExit(
            f"No units found under {PRS_DIR}. Run `phantombench scrape` and `phantombench inject` first."
        )
    if unit_id is None:
        return units

    if "/" in unit_id:
        pr_str, _, stem = unit_id.partition("/")
        matches = [u for u in units if str(u.pr_number) == pr_str and u.defect_stem == stem]
    else:
        matches = [u for u in units if u.defect_stem == unit_id]

    if not matches:
        raise SystemExit(
            f"No unit matches --unit {unit_id!r}. Use '<defect_stem>' for an injected unit "
            "(e.g. 001-exclude-none) or '<pr_number>/clean' for a clean unit (e.g. 1811/clean)."
        )
    if len(matches) > 1:
        options = ", ".join(f"{u.pr_number}/{u.defect_stem}" for u in matches)
        raise SystemExit(f"--unit {unit_id!r} is ambiguous, matches: {options}")
    return matches


def _resolve_models(config: Config, model_id: str | None) -> list[ModelConfig]:
    if model_id is None:
        return config.models
    for m in config.models:
        if m.id == model_id:
            return [m]
    raise SystemExit(f"No model {model_id!r} in config.yaml. Known ids: {[m.id for m in config.models]}")


def _build_prompt(diff: str) -> str:
    rubric = RUBRIC_PATH.read_text()
    return f"{rubric}\n\n```diff\n{diff}\n```\n"


def _call_openrouter(
    api_key: str,
    model: ModelConfig,
    prompt: str,
    review_cfg: dict,
    max_retries: int = MAX_RETRIES,
    timeout: float = 120.0,
) -> tuple[dict, float]:
    # max_retries/timeout are overridable so the live demo (§7) can trade the
    # full-run retry budget — whose backoff alone can exceed a minute — for a
    # hard deadline it can finish inside.
    client = httpx.Client(
        base_url="https://openrouter.ai/api/v1",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    last_error = ""
    with client:
        for attempt in range(max_retries + 1):
            start = time.monotonic()
            try:
                resp = client.post(
                    "/chat/completions",
                    json={
                        "model": model.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": review_cfg["temperature"],
                        "max_tokens": review_cfg["max_output_tokens"],
                        "reasoning": {"effort": review_cfg["reasoning_effort"]},
                    },
                )
            except httpx.TransportError as exc:
                last_error = f"transport error: {exc}"
            else:
                latency = time.monotonic() - start
                if resp.status_code == 200:
                    return resp.json(), latency
                if resp.status_code not in TRANSIENT_STATUS_CODES:
                    raise SystemExit(f"OpenRouter request failed ({resp.status_code}): {resp.text}")
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"

            if attempt < max_retries:
                delay = BACKOFF_BASE_SECONDS * (2**attempt)
                console.print(
                    f"    [yellow]retry {attempt + 1}/{max_retries}[/] after {last_error} "
                    f"— waiting {delay:.0f}s"
                )
                time.sleep(delay)

    raise RunError(last_error)


def _parse_findings(raw_content: str | None) -> list[dict] | None:
    if not raw_content:
        return None
    text = raw_content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    return parsed


def _persist_run(
    unit: Unit, model: ModelConfig, response: dict, latency: float, runs_dir: Path = RUNS_DIR
) -> Path:
    # runs_dir is overridable so the live demo can persist its fresh responses
    # somewhere other than data/runs/ — that tree is the scored corpus and a
    # demo re-run would otherwise overwrite the very files the numbers came from.
    run_dir = runs_dir / str(unit.pr_number) / unit.defect_stem
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path = run_dir / f"{model.id}.json"

    choice = response.get("choices", [{}])[0]
    raw_content = choice.get("message", {}).get("content", "")
    usage = response.get("usage", {})

    record = {
        "model_id": model.id,
        "model": model.model,
        "provider": model.provider,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "latency_seconds": round(latency, 3),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "raw_response": response,
        "raw_content": raw_content,
    }
    out_path.write_text(json.dumps(record, indent=2))
    return out_path


def _print_comparison(unit: Unit, model: ModelConfig, record: dict, findings: list[dict] | None) -> None:
    console.print()
    console.rule(f"PR #{unit.pr_number} — {unit.defect_stem} — {model.id}")

    gt = unit.ground_truth
    if gt is None:
        console.print("[bold]Ground truth[/] — clean unit, no injected defect")
    else:
        console.print(f"[bold]Ground truth[/] ({gt['defect_class']}, {gt['file']}:{gt['line_start']}-{gt['line_end']})")
        console.print(f"  {gt['summary'].strip()}")
        console.print(f"  [dim]detection hint: {gt['detection_hint'].strip()}[/]")
    console.print()

    console.print(f"[bold]{model.id} findings[/]")
    if findings is None:
        console.print("  [red]could not parse model output as JSON — raw content:[/]")
        console.print(f"  {record['raw_content'][:2000]}")
    elif not findings:
        console.print("  [red](none — model reported no findings)[/]" if gt else "  (none)")
    else:
        for f in findings:
            console.print(
                f"  [{f.get('severity', '?')}] {f.get('file', '?')}:{f.get('line', '?')} "
                f"— {f.get('failure_mode', '')}"
            )
            if f.get("impact"):
                console.print(f"      impact: {f['impact']}")
            if f.get("suggested_fix"):
                console.print(f"      fix: {f['suggested_fix']}")


def run(config: Config, model_id: str | None = None, unit_id: str | None = None) -> None:
    units = _resolve_units(unit_id)
    models = _resolve_models(config, model_id)
    single = len(units) == 1 and len(models) == 1

    api_key: str | None = None
    total = len(units) * len(models)
    succeeded = 0
    call_failures: list[str] = []
    parse_failures: list[str] = []

    for unit in units:
        for model in models:
            label = f"PR #{unit.pr_number}/{unit.defect_stem}/{model.id}"
            out_path = RUNS_DIR / str(unit.pr_number) / unit.defect_stem / f"{model.id}.json"

            if out_path.exists():
                record = json.loads(out_path.read_text())
                if not single:
                    console.print(f"[cyan]cached[/] {label}")
            else:
                if api_key is None:
                    api_key = _require_openrouter_key()
                prompt = _build_prompt(unit.diff)
                if not single:
                    console.print(f"[yellow]calling[/] {label}...")
                try:
                    response, latency = _call_openrouter(api_key, model, prompt, config.review)
                except RunError as exc:
                    console.print(f"[red]failed[/] {label}: {exc}")
                    call_failures.append(label)
                    continue
                out_path = _persist_run(unit, model, response, latency)
                record = json.loads(out_path.read_text())
                console.print(f"[green]reviewed[/] {label} -> {out_path}")
                if not single:
                    console.print(
                        f"  {record['prompt_tokens']} prompt / {record['completion_tokens']} completion tokens, "
                        f"{record['latency_seconds']}s"
                    )

            findings = _parse_findings(record["raw_content"])
            if findings is None:
                console.print(f"[red]schema violation[/] {label}: model output did not parse as a JSON findings array")
                parse_failures.append(label)

            succeeded += 1

            if single:
                _print_comparison(unit, model, record, findings)

    if not single:
        console.print()
        console.rule("run summary")
        console.print(f"{succeeded}/{total} unit/model pairs have a persisted response on disk")
        if call_failures:
            console.print(
                f"[red]{len(call_failures)} call failures[/] (not persisted — re-run the same command to retry): "
                + ", ".join(call_failures)
            )
        if parse_failures:
            console.print(
                f"[yellow]{len(parse_failures)} schema violations[/] (persisted but unparsable as JSON): "
                + ", ".join(parse_failures)
            )
