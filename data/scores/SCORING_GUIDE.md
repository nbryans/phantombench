# Scoring guide

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
