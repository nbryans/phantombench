# Who Reviews the Reviewer? — results

All 104 findings carry a human-verified `tag`.

Paired design: 12 injected defects and the same 12 PRs unmodified, reviewed by 3 models from the diff alone.

## Catch rate per model

| model | described | localized-or-better | missed | `unrelated` findings on injected PRs |
|---|---|---|---|---|
| claude | 11/12 (92%) | 11/12 (92%) | 1 | 3 |
| gpt | 12/12 (100%) | 12/12 (100%) | 0 | 3 |
| gemini | 8/12 (67%) | 10/12 (83%) | 2 | 11 |

## Catch rate per defect class

| defect class | claude | gpt | gemini |
|---|---|---|---|
| `async_lifecycle` | 2/2 | 2/2 | 1/2 |
| `contract_violation` | 3/3 | 3/3 | 2/3 |
| `local_mechanical` | 3/3 | 3/3 | 3/3 |
| `security` | 2/2 | 2/2 | 1/2 |
| `silent_semantic` | 1/2 | 2/2 | 1/2 |

## False positives on the 12 clean control PRs

| model | false positives | per clean PR | clean PRs with ≥1 FP | true findings | nits | schema violations |
|---|---|---|---|---|---|---|
| claude | 10 | 0.83 | 6/12 | 4 | 1 | 0 |
| gpt | 7 | 0.58 | 4/12 | 0 | 0 | 0 |
| gemini | 14 | 1.17 | 10/12 | 0 | 0 | 0 |

Clean control PRs are real merged code and can contain genuine pre-existing bugs; those are tagged `true_finding` and excluded from the false-positive count.

## Overlap — how many models described each defect

| defect class | caught by 3 | by 2 | by 1 | by none |
|---|---|---|---|---|
| `async_lifecycle` | 1 | 1 | 0 | 0 |
| `contract_violation` | 2 | 1 | 0 | 0 |
| `local_mechanical` | 3 | 0 | 0 | 0 |
| `security` | 1 | 1 | 0 | 0 |
| `silent_semantic` | 1 | 0 | 1 | 0 |
| **all** | **8** | **3** | **1** | **0** |

## Cost and latency

| model | calls | total cost | median latency | slowest | prompt tokens | completion tokens | of which reasoning |
|---|---|---|---|---|---|---|---|
| claude | 24 | $0.50 | 15.9s | 31.8s | 62,917 | 20,708 | 14,006 |
| gpt | 24 | $0.47 | 16.6s | 103.7s | 48,972 | 40,535 | 35,776 |
| gemini | 24 | $0.37 | 12.2s | 17.0s | 59,731 | 29,325 | 18,068 |

Total spend across the full matrix: **$1.33**.

## Charts

- `catch_vs_fp.png` — catch rate vs. false positives per clean PR
- `overlap.png` — overlap by defect class
