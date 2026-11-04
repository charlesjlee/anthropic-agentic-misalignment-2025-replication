# Historical results and run logs

Completed October 29, 2026: **2,400 subject responses and 2,400 Astra judgments**, across four models and six conditions. The tables use the original action gate, intent parser and final scoring rule.

| What to inspect | Saved artifact |
| --- | --- |
| Results, completeness, and costs | `evidence/historical/historical-20261029T1555352673760Z/analysis/report.md` (Report) |
| Subject responses and judge explanations | `evidence/historical/historical-20261029T1555352673760Z/transcripts.md` (Transcripts) |
| Exact prompts and generation settings | `evidence/historical/historical-20261029T1555352673760Z/prompts.json` (Prompts), `evidence/historical/historical-20261029T1555352673760Z/plan.json` (plan) |
| Per-response requests, outputs, and scores | `evidence/historical/historical-20261029T1555352673760Z/trials/` (Trial records) |
| Progress and error logs | `evidence/historical/historical-20261029T1555352673760Z/execution-logs/` (Execution logs) |
| Completion and integrity | `evidence/historical/historical-20261029T1555352673760Z/completion-receipt.json` (Completion receipt), `evidence/historical/historical-20261029T1555352673760Z/index.json` (file checksums) |
| Detector versus intent counts | [Scoring diagnostics](scoring-diagnostics.md) |
| Recorded judge reasoning settings | [Settings audit](judge-settings-audit.json) |

The paths above refer to files in the historical release archive, including validation and review evidence. See the [release manifest and extraction instructions](../../release/README.md).

The reproduction code, saved scores and reports use only the original classifier's action gate, intent parser and final rule.

The `evidence/historical/historical-20261029T1555352673760Z/RUN.md` (original run history) remains inside the frozen evidence, including recovery details. No completed paid subject was regenerated. To verify the saved files or rebuild the tables, use the [reproduction guide](README.md).

Costs in the root README are usage values, not additional out-of-pocket charges after credits. OpenAI values are estimates before cache discounts; OpenRouter values are reported usage. They exclude validation, subscriptions, credit-purchase fees, and taxes. Credit coverage is reported by the account owner; billing allocation was not independently audited.
