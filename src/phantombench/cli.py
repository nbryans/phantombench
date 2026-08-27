import argparse
import sys
from pathlib import Path

from phantombench import annotate, demo, inject, report, review, scrape, score
from phantombench.config import DEFAULT_CONFIG_PATH, load_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="phantombench")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    p_scrape = sub.add_parser("scrape", help="pull merged PRs from the target repo")
    p_scrape.add_argument("--pr", type=int, default=None)
    p_scrape.add_argument("--batch", type=int, default=None, help="persist the first N fitting candidates instead of just one")

    p_inject = sub.add_parser("inject", help="apply a defect patch and verify diff containment")
    p_inject.add_argument("--defect", type=str, default=None)

    p_review = sub.add_parser("review", help="send units to models, persist raw responses")
    p_review.add_argument("--model", type=str, default=None, help="single model id (default: all configured models)")
    p_review.add_argument(
        "--unit",
        type=str,
        default=None,
        help="single unit, e.g. 001-exclude-none or 1811/clean (default: every injected and clean unit on disk)",
    )

    sub.add_parser("score", help="emit a hand-scoring worksheet")
    p_annotate = sub.add_parser("annotate", help="local web UI for hand-scoring the worksheet")
    p_annotate.add_argument("--port", type=int, default=8765)
    p_annotate.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    sub.add_parser("report", help="compute tables and charts from scores")
    p_demo = sub.add_parser("demo", help="run a small curated subset live")
    p_demo.add_argument(
        "--replay",
        action="store_true",
        help="serve the demo from data/runs/ at recorded pacing, zero network calls",
    )
    p_demo.add_argument(
        "--speed",
        type=float,
        default=None,
        help="--replay pacing multiplier (2.0 = twice as fast; default from config.yaml)",
    )

    args = parser.parse_args()
    config = load_config(args.config)

    if args.command == "scrape":
        scrape.run(config, pr_number=args.pr, batch=args.batch)
    elif args.command == "inject":
        inject.run(config, defect_id=args.defect)
    elif args.command == "review":
        review.run(config, model_id=args.model, unit_id=args.unit)
    elif args.command == "score":
        score.run(config)
    elif args.command == "annotate":
        annotate.run(config, port=args.port, open_browser=not args.no_open)
    elif args.command == "report":
        try:
            report.run(config)
        except report.ReportError as exc:
            report.console.print(f"\n[bold red]report stopped:[/] {exc}")
            sys.exit(1)
    elif args.command == "demo":
        # §7: exit non-zero and print something legible on failure — a stack
        # trace must never be the thing on screen during the talk.
        try:
            demo.run(config, replay=args.replay, speed=args.speed)
        except demo.DemoError as exc:
            demo.console.print(f"\n[bold red]demo stopped:[/] {exc}")
            sys.exit(1)
        except KeyboardInterrupt:
            demo.console.print("\n[yellow]demo interrupted.[/]")
            sys.exit(1)
        except Exception as exc:  # noqa: BLE001
            demo.console.print(f"\n[bold red]demo failed:[/] {exc.__class__.__name__}: {exc}")
            demo.console.print("[dim]Try `phantombench demo --replay` — it needs no network.[/]")
            sys.exit(1)


if __name__ == "__main__":
    main()
