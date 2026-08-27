from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from rich.console import Console

from phantombench.config import Config

GITHUB_API = "https://api.github.com"
DATA_DIR = Path("data/prs")
ENV_PATH = Path(".env")

# Titles that are always noise, regardless of what a given config.yaml excludes.
BUILTIN_TITLE_EXCLUDES = (re.compile(r"^(Revert|Merge)\b", re.IGNORECASE),)

console = Console()


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


def _require_github_token() -> str:
    _load_dotenv()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit(
            "GITHUB_TOKEN is not set. Copy .env.example to .env and fill it in "
            "(create a token with no special scopes at https://github.com/settings/tokens)."
        )
    return token


def _client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=GITHUB_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30.0,
    )


def _get(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    resp = client.get(url, **kwargs)
    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        raise SystemExit(f"GitHub API rate limit exhausted. Response: {resp.text}")
    resp.raise_for_status()
    return resp


def _is_source_file(path: str) -> bool:
    return path.endswith(".py")


def _title_excluded(title: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(title) for p in (*BUILTIN_TITLE_EXCLUDES, *patterns))


def _iter_merged_prs(client: httpx.Client, owner: str, repo: str, scan_limit: int):
    page = 1
    scanned = 0
    while scanned < scan_limit:
        resp = _get(
            client,
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "closed", "sort": "updated", "direction": "desc", "per_page": 50, "page": page},
        )
        items = resp.json()
        if not items:
            return
        for item in items:
            if scanned >= scan_limit:
                return
            scanned += 1
            if item.get("merged_at"):
                yield item
        page += 1


def _fits_criteria(pr: dict, files: list[dict], scrape_cfg: dict, title_excludes: list[re.Pattern]) -> bool:
    if _title_excluded(pr["title"], title_excludes):
        return False
    changed_files = pr["changed_files"]
    changed_lines = pr["additions"] + pr["deletions"]
    if not (scrape_cfg["min_changed_files"] <= changed_files <= scrape_cfg["max_changed_files"]):
        return False
    if not (scrape_cfg["min_changed_lines"] <= changed_lines <= scrape_cfg["max_changed_lines"]):
        return False
    if not any(_is_source_file(f["filename"]) for f in files):
        return False
    return True


def _fetch_file_content(client: httpx.Client, owner: str, repo: str, path: str, ref: str) -> bytes | None:
    resp = _get(client, f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}", params={"ref": ref})
    body = resp.json()
    if body.get("encoding") == "base64" and body.get("content") is not None:
        return base64.b64decode(body["content"])
    download_url = body.get("download_url")
    if download_url:
        raw = httpx.get(download_url, timeout=30.0)
        raw.raise_for_status()
        return raw.content
    return None


def _persist(client: httpx.Client, owner: str, repo: str, pr: dict, files: list[dict]) -> Path:
    pr_dir = DATA_DIR / str(pr["number"])
    files_dir = pr_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    diff_resp = _get(
        client,
        f"/repos/{owner}/{repo}/pulls/{pr['number']}",
        headers={"Accept": "application/vnd.github.v3.diff"},
    )
    (pr_dir / "diff.patch").write_text(diff_resp.text)

    file_records = []
    for f in files:
        record = {
            "path": f["filename"],
            "status": f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
            "is_source": _is_source_file(f["filename"]),
        }
        if f["status"] != "removed":
            content = _fetch_file_content(client, owner, repo, f["filename"], pr["head"]["sha"])
            if content is not None:
                dest = files_dir / f["filename"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
        file_records.append(record)

    meta = {
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["html_url"],
        "base_sha": pr["base"]["sha"],
        "head_sha": pr["head"]["sha"],
        "merged_at": pr["merged_at"],
        "additions": pr["additions"],
        "deletions": pr["deletions"],
        "changed_lines": pr["additions"] + pr["deletions"],
        "changed_files_count": pr["changed_files"],
        "files": file_records,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }
    (pr_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return pr_dir


def _report(pr_dir: Path, cached: bool) -> None:
    meta = json.loads((pr_dir / "meta.json").read_text())
    console.print(f"[{'cyan' if cached else 'green'}]{'cached' if cached else 'scraped'}[/] "
                  f"PR #{meta['number']}: {meta['title']}")
    console.print(f"  {meta['url']}")
    console.print(f"  {meta['changed_files_count']} files, {meta['changed_lines']} changed lines")
    console.print(f"  base {meta['base_sha'][:10]}  head {meta['head_sha'][:10]}")
    console.print(f"  -> {pr_dir}/")


def _run_batch(config: Config, limit: int) -> None:
    token = _require_github_token()
    scrape_cfg = config.scrape
    title_excludes = [re.compile(p) for p in scrape_cfg.get("exclude_title_patterns", [])]
    scan_limit = scrape_cfg.get("over_fetch", 40)

    found = 0
    with _client(token) as client:
        owner, repo = config.repo_owner, config.repo_name
        for candidate in _iter_merged_prs(client, owner, repo, scan_limit):
            if found >= limit:
                break
            if _title_excluded(candidate["title"], title_excludes):
                continue
            pr_dir = DATA_DIR / str(candidate["number"])
            if (pr_dir / "meta.json").exists():
                _report(pr_dir, cached=True)
                found += 1
                continue
            pr = _get(client, f"/repos/{owner}/{repo}/pulls/{candidate['number']}").json()
            files = _get(
                client, f"/repos/{owner}/{repo}/pulls/{candidate['number']}/files", params={"per_page": 100}
            ).json()
            if not _fits_criteria(pr, files, scrape_cfg, title_excludes):
                continue
            pr_dir = _persist(client, owner, repo, pr, files)
            _report(pr_dir, cached=False)
            found += 1

    if found == 0:
        raise SystemExit(
            f"No merged PR among the {scan_limit} most recently updated matched the scrape criteria in config.yaml."
        )
    console.print(f"[bold]batch complete[/]: {found}/{limit} fitting candidates persisted to {DATA_DIR}/")


def run(config: Config, pr_number: int | None = None, batch: int | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if batch is not None:
        _run_batch(config, batch)
        return

    if pr_number is not None:
        pr_dir = DATA_DIR / str(pr_number)
        if (pr_dir / "meta.json").exists():
            _report(pr_dir, cached=True)
            return

        token = _require_github_token()
        with _client(token) as client:
            owner, repo = config.repo_owner, config.repo_name
            pr = _get(client, f"/repos/{owner}/{repo}/pulls/{pr_number}").json()
            if not pr.get("merged_at"):
                raise SystemExit(f"PR #{pr_number} is not merged; scrape only works on merged PRs.")
            files = _get(client, f"/repos/{owner}/{repo}/pulls/{pr_number}/files", params={"per_page": 100}).json()
            pr_dir = _persist(client, owner, repo, pr, files)
        _report(pr_dir, cached=False)
        return

    existing = sorted(DATA_DIR.glob("*/meta.json"))
    if existing:
        _report(existing[0].parent, cached=True)
        return

    token = _require_github_token()
    scrape_cfg = config.scrape
    title_excludes = [re.compile(p) for p in scrape_cfg.get("exclude_title_patterns", [])]
    scan_limit = scrape_cfg.get("over_fetch", 40)

    with _client(token) as client:
        owner, repo = config.repo_owner, config.repo_name
        for candidate in _iter_merged_prs(client, owner, repo, scan_limit):
            if _title_excluded(candidate["title"], title_excludes):
                continue
            pr = _get(client, f"/repos/{owner}/{repo}/pulls/{candidate['number']}").json()
            files = _get(
                client, f"/repos/{owner}/{repo}/pulls/{candidate['number']}/files", params={"per_page": 100}
            ).json()
            if not _fits_criteria(pr, files, scrape_cfg, title_excludes):
                continue
            pr_dir = _persist(client, owner, repo, pr, files)
            _report(pr_dir, cached=False)
            return

    raise SystemExit(
        f"No merged PR among the {scan_limit} most recently updated matched the scrape criteria in config.yaml."
    )
