from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from rich.console import Console

from phantombench.config import Config

PRS_DIR = Path("data/prs")
DEFECTS_DIR = Path("defects")

console = Console()

HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Defect:
    stem: str
    id: str
    pr_number: int
    file: str
    line_start: int
    line_end: int
    defect_class: str
    summary: str
    detection_hint: str
    patch_path: Path
    yaml_path: Path


def _resolve_defect(defect_id: str | None) -> Defect:
    if defect_id is not None:
        matches = sorted(DEFECTS_DIR.glob(f"{defect_id}*.yaml"))
    else:
        matches = sorted(DEFECTS_DIR.glob("*.yaml"))

    if not matches:
        raise SystemExit(f"No defect YAML found in {DEFECTS_DIR}/ matching {defect_id!r}.")
    if defect_id is not None and len(matches) > 1:
        raise SystemExit(f"Ambiguous defect id {defect_id!r}: matches {[m.name for m in matches]}")

    yaml_path = matches[0]
    stem = yaml_path.stem
    patch_path = DEFECTS_DIR / f"{stem}.patch"
    if not patch_path.exists():
        raise SystemExit(f"Defect {stem!r} has a YAML file but no matching patch at {patch_path}.")

    raw = yaml.safe_load(yaml_path.read_text())
    return Defect(
        stem=stem,
        id=str(raw["id"]),
        pr_number=int(raw["pr_number"]),
        file=raw["file"],
        line_start=int(raw["line_start"]),
        line_end=int(raw["line_end"]),
        defect_class=raw["defect_class"],
        summary=raw["summary"].strip(),
        detection_hint=raw["detection_hint"].strip(),
        patch_path=patch_path,
        yaml_path=yaml_path,
    )


def _run_git_apply(patch_path: Path, cwd: Path, reverse: bool) -> None:
    cmd = ["git", "apply"]
    if reverse:
        cmd.append("-R")
    cmd.append(str(patch_path.resolve()))
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        direction = "reverse-apply" if reverse else "apply"
        raise SystemExit(
            f"Failed to {direction} {patch_path} in {cwd}:\n{result.stdout}{result.stderr}"
        )


def _changed_file_paths(meta: dict) -> list[str]:
    return [f["path"] for f in meta["files"] if f["status"] != "removed"]


def _seed_tree(pr_dir: Path, dest: Path, paths: list[str]) -> None:
    for path in paths:
        src = pr_dir / "files" / path
        dst = dest / path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _unified_diff_for_file(path: str, base_dir: Path, injected_dir: Path) -> str:
    base_file = base_dir / path
    injected_file = injected_dir / path
    base_lines = base_file.read_text().splitlines(keepends=True) if base_file.exists() else []
    injected_lines = injected_file.read_text().splitlines(keepends=True)
    if base_lines == injected_lines:
        return ""
    diff_lines = list(
        difflib.unified_diff(
            base_lines,
            injected_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    if not diff_lines:
        return ""
    body = "".join(diff_lines)
    if not body.endswith("\n"):
        body += "\n"
    return f"diff --git a/{path} b/{path}\n{body}"


def _hunks_for_file(regenerated_diff: str, path: str) -> list[str]:
    marker = f"diff --git a/{path} b/{path}\n"
    start = regenerated_diff.find(marker)
    if start == -1:
        return []
    end = regenerated_diff.find("\ndiff --git a/", start + 1)
    section = regenerated_diff[start:] if end == -1 else regenerated_diff[start:end]
    return [line for line in section.splitlines() if line.startswith("@@")]


def _assert_diff_containment(regenerated_diff: str, defect: Defect) -> None:
    hunks = _hunks_for_file(regenerated_diff, defect.file)
    if not hunks:
        raise SystemExit(
            f"Diff-containment check failed: no hunk for {defect.file} in the regenerated diff. "
            "The injected defect does not appear in base -> (head + defect) at all."
        )

    covered_ranges = []
    for hunk in hunks:
        match = HUNK_HEADER_RE.match(hunk)
        if not match:
            raise SystemExit(f"Could not parse hunk header: {hunk!r}")
        new_start = int(match.group(1))
        new_count = int(match.group(2)) if match.group(2) is not None else 1
        covered_ranges.append((new_start, new_start + max(new_count, 1) - 1))

    in_range = any(
        lo <= defect.line_start and defect.line_end <= hi for lo, hi in covered_ranges
    )
    if not in_range:
        raise SystemExit(
            f"Diff-containment check failed: defect lines {defect.line_start}-{defect.line_end} "
            f"in {defect.file} do not fall inside any regenerated hunk range {covered_ranges}. "
            "A diff-only reviewer could not possibly see this defect."
        )


def run(config: Config, defect_id: str | None = None) -> None:
    defect = _resolve_defect(defect_id)

    pr_dir = PRS_DIR / str(defect.pr_number)
    meta_path = pr_dir / "meta.json"
    if not meta_path.exists():
        raise SystemExit(
            f"No scraped data for PR #{defect.pr_number} at {pr_dir}/. "
            f"Run `phantombench scrape --pr {defect.pr_number}` first."
        )
    meta = json.loads(meta_path.read_text())
    paths = _changed_file_paths(meta)
    if defect.file not in paths:
        raise SystemExit(
            f"Defect {defect.stem!r} targets {defect.file!r}, which is not among PR #{defect.pr_number}'s "
            f"changed files: {paths}"
        )

    with tempfile.TemporaryDirectory(prefix="phantombench-inject-") as tmp:
        tmp_path = Path(tmp)
        base_dir = tmp_path / "base"
        injected_dir = tmp_path / "injected"
        base_dir.mkdir()
        injected_dir.mkdir()

        _seed_tree(pr_dir, base_dir, paths)
        _run_git_apply(pr_dir / "diff.patch", cwd=base_dir, reverse=True)

        _seed_tree(pr_dir, injected_dir, paths)
        _run_git_apply(defect.patch_path, cwd=injected_dir, reverse=False)

        regenerated_diff = "".join(
            _unified_diff_for_file(path, base_dir, injected_dir) for path in paths
        )

        _assert_diff_containment(regenerated_diff, defect)

        out_dir = pr_dir / "injected" / defect.stem
        out_files_dir = out_dir / "files"
        if out_files_dir.exists():
            shutil.rmtree(out_files_dir)
        out_files_dir.mkdir(parents=True)
        for path in paths:
            dst = out_files_dir / path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(injected_dir / path, dst)

        (out_dir / "diff.patch").write_text(regenerated_diff)
        shutil.copy2(defect.yaml_path, out_dir / "ground_truth.yaml")

    console.print(f"[green]injected[/] defect {defect.stem!r} ({defect.defect_class}) into PR #{defect.pr_number}")
    console.print(f"  {defect.file}:{defect.line_start}-{defect.line_end}")
    console.print(f"  diff-containment check: [green]passed[/]")
    console.print(f"  -> {out_dir}/")
