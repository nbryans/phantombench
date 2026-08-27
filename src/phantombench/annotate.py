"""Local annotation UI for hand-scoring the worksheet.

Serves a side-by-side diff view of every unit with the model findings
anchored to their lines, and writes `tag`/`notes` straight back into
`data/scores/worksheet.csv` keyed the same way `score.py` merges rows —
so `phantombench score` can still be re-run safely at any time.

Blind scoring: `draft_tag`/`draft_rationale` are withheld from every API
response until the row has a human tag; the save response reveals them.
"""

import csv
import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml
from rich.console import Console

from phantombench.config import Config
from phantombench.review import PRS_DIR
from phantombench.score import CLEAN_TAGS, INJECTED_TAGS, PLACEHOLDER_INDEX, WORKSHEET_PATH

HTML_PATH = Path(__file__).parent / "annotate.html"
ALLOWED_TAGS = INJECTED_TAGS | CLEAN_TAGS | {""}

HUNK_RE = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

console = Console()


def _parse_diff(diff_text: str) -> list[dict]:
    """Unified git diff -> [{path, hunks: [{header, lines}]}], with old/new
    line numbers attached so the frontend can pair rows side-by-side."""
    files: list[dict] = []
    current: dict | None = None
    hunk: dict | None = None
    old_ln = new_ln = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            current = {"path": "", "hunks": []}
            files.append(current)
            hunk = None
        elif line.startswith("+++ "):
            if current is not None:
                path = line[4:]
                current["path"] = path[2:] if path.startswith("b/") else path
        elif line.startswith("--- "):
            continue
        elif line.startswith("@@") and current is not None:
            m = HUNK_RE.match(line)
            if not m:
                continue
            old_ln, new_ln = int(m.group(1)), int(m.group(2))
            hunk = {"header": line, "lines": []}
            current["hunks"].append(hunk)
        elif hunk is not None:
            if line.startswith("+"):
                hunk["lines"].append({"t": "add", "new": new_ln, "text": line[1:]})
                new_ln += 1
            elif line.startswith("-"):
                hunk["lines"].append({"t": "del", "old": old_ln, "text": line[1:]})
                old_ln += 1
            elif line.startswith("\\"):
                continue  # "\ No newline at end of file"
            else:
                hunk["lines"].append(
                    {"t": "ctx", "old": old_ln, "new": new_ln, "text": line[1:] if line else ""}
                )
                old_ln += 1
                new_ln += 1
    return files


class Worksheet:
    """The worksheet held in memory, written back atomically on every save."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            self.fieldnames = list(reader.fieldnames or [])
            self.rows = list(reader)

    def save_tag(self, unit_id: str, model_id: str, finding_index: int, tag: str, notes: str) -> dict:
        with self.lock:
            for row in self.rows:
                if (
                    row["unit_id"] == unit_id
                    and row["model_id"] == model_id
                    and int(row["finding_index"]) == finding_index
                ):
                    row["tag"] = tag
                    row["notes"] = notes
                    self._write()
                    return row
            raise KeyError(f"no row for {unit_id}/{model_id}/{finding_index}")

    def _write(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.fieldnames)
            writer.writeheader()
            writer.writerows(self.rows)
        tmp.replace(self.path)


def _unit_extras(unit_id: str, unit_type: str) -> dict:
    """PR title/url from meta.json, diff from disk, gt line range from yaml."""
    pr_str, _, stem = unit_id.partition("/")
    pr_dir = PRS_DIR / pr_str
    extras: dict = {"pr_title": "", "pr_url": "", "diff": [], "gt_range": None}

    meta_path = pr_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        extras["pr_title"] = meta.get("title", "")
        extras["pr_url"] = meta.get("url", "")

    diff_path = pr_dir / "diff.patch" if unit_type == "clean" else pr_dir / "injected" / stem / "diff.patch"
    if diff_path.exists():
        extras["diff"] = _parse_diff(diff_path.read_text())

    gt_path = pr_dir / "injected" / stem / "ground_truth.yaml"
    if unit_type == "injected" and gt_path.exists():
        gt = yaml.safe_load(gt_path.read_text())
        extras["gt_range"] = [gt["line_start"], gt["line_end"]]

    return extras


def _row_payload(row: dict) -> dict:
    """One worksheet row for the frontend — drafts withheld until tagged."""
    payload = {
        "model_id": row["model_id"],
        "finding_index": int(row["finding_index"]),
        "severity": row["severity"],
        "finding_file": row["finding_file"],
        "finding_line": row["finding_line"],
        "failure_mode": row["failure_mode"],
        "impact": row["impact"],
        "suggested_fix": row["suggested_fix"],
        "raw_content": row["raw_content"],
        "tag": row["tag"],
        "notes": row["notes"],
    }
    if row["tag"]:
        payload["draft_tag"] = row.get("draft_tag", "")
        payload["draft_rationale"] = row.get("draft_rationale", "")
    return payload


def _state(sheet: Worksheet) -> dict:
    units: dict[str, dict] = {}
    for row in sheet.rows:
        unit = units.get(row["unit_id"])
        if unit is None:
            unit = {
                "unit_id": row["unit_id"],
                "pr_number": int(row["pr_number"]),
                "unit_type": row["unit_type"],
                "defect_class": row["defect_class"],
                "gt_file": row["gt_file"],
                "gt_line": row["gt_line"],
                "gt_summary": row["gt_summary"],
                "detection_hint": row["detection_hint"],
                "rows": [],
                **_unit_extras(row["unit_id"], row["unit_type"]),
            }
            units[row["unit_id"]] = unit
        unit["rows"].append(_row_payload(row))
    return {
        "worksheet": str(sheet.path),
        "placeholder_index": PLACEHOLDER_INDEX,
        "injected_tags": sorted(INJECTED_TAGS),
        "clean_tags": sorted(CLEAN_TAGS),
        "units": list(units.values()),
    }


def _make_handler(sheet: Worksheet):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, obj: dict, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/", "/index.html"):
                self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send_json(_state(sheet))
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/tag":
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
                tag = body["tag"]
                if tag not in ALLOWED_TAGS:
                    self._send_json({"error": f"unknown tag {tag!r}"}, code=400)
                    return
                row = sheet.save_tag(
                    body["unit_id"],
                    body["model_id"],
                    int(body["finding_index"]),
                    tag,
                    body.get("notes", ""),
                )
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, code=400)
                return
            reveal = {}
            if row["tag"]:
                reveal = {
                    "draft_tag": row.get("draft_tag", ""),
                    "draft_rationale": row.get("draft_rationale", ""),
                }
            self._send_json({"ok": True, **reveal})

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            pass  # keep the terminal quiet while clicking around

    return Handler


def run(config: Config, port: int = 8765, open_browser: bool = True) -> None:
    if not WORKSHEET_PATH.exists():
        raise SystemExit(f"{WORKSHEET_PATH} not found. Run `phantombench score` first.")

    sheet = Worksheet(WORKSHEET_PATH)
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(sheet))
    url = f"http://127.0.0.1:{port}/"
    console.print(f"[green]annotate UI:[/] {url}  [dim](Ctrl-C to stop; saves go straight into {WORKSHEET_PATH})[/]")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[yellow]annotate stopped.[/]")
