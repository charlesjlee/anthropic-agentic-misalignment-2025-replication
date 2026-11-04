# Newer-model update reproduction

Run commands from the repository root. For environment setup, see the [historical reproduction guide](../historical/README.md).

## Final results and offline reproduction

All 15 update models completed 100 trials in each of six conditions (9,000 trials), with zero scored harmful actions. The [verification summary](summary.json) records the replay result. These are original automated scores, not independent human adjudications or a general safety guarantee.

The [archive manifest](archive.json) records the filename, size and SHA-256 of `datacenter-update-2026-11-03.zip`. The ZIP contains `experiments/update/archive-index.json` and `trials.json`; the verifier checks every indexed file and replays each trial. See the [release asset guide](../../release/README.md). The historical archive remains `datacenter-historical-2026-10-29.tar.gz`, described in its [storage audit](../historical/storage-audit.json).

After obtaining the update archive, extract it to a separate directory. The archive preserves repository-relative paths, including an evidence-only `.local/expansion/` subtree; it contains no authentication directory. With the repository's pinned Python environment installed:

```powershell
python experiments/update/run.py verify --root C:/path/to/extracted-update
```

Verification checks indexed file hashes, completed receipts and OpenAI terminal streams, then reruns the pinned original scorer on all 9,000 saved actor/judge pairs. It checks 100 distinct samples per model-condition cell and compares the result with the exported trial records. It does not make API calls, read credentials or regenerate outputs. `python experiments/update/run.py verify --archive path/to/datacenter-update-2026-11-03.zip` extracts an isolated copy, verifies it and removes the temporary extraction. The archived file index describes the portable evidence copy. Workstation paths and dependent integrity references are normalized; `privacy-redactions.json` records the transformation. Visible model responses and behavioral scores are retained.

## Figure

The [combined snapshot](../../docs/results/combined-results-2026-11-03.json) records the configuration IDs, six-condition counts, costs and source hashes used in the figure. The [CSV](../../docs/results/combined-results-2026-11-03.csv) has one row per model and condition. Rebuild the graph from that snapshot without credentials or model calls:

```powershell
uv run --no-project --python 3.11 --with matplotlib==3.10.7 --with numpy==2.3.4 python scripts/build-readme-results.py
```

To refresh the snapshot from the local run ledger before rebuilding:

```powershell
.\.venv\Scripts\python.exe scripts/build-readme-results.py --refresh-from-ledger --export-only
```

The refresh reads `.local/expansion/ledger.sqlite3`; it does not launch inference. It includes only complete 600-trial configurations in the graph including Gemini 4 Argon. The CSV keeps each stacked model separate. The snapshot preserves costs for pending and discarded runs as well as completed runs.

## Executed protocol and live reruns

The campaign directories linked below are retained locally and in the release archive, rather than as tens of thousands of loose Git files.

| Update stage | Frozen settings | Responses, scores and execution records |
| --- | --- | --- |
| First Sol/Astra cohort | [Expansion manifest](../../configs/expansion-smoke-v1.json) | `experiments/openai-expansion/phase3-report.txt` (Report), `experiments/openai-expansion/phase3-full-20261102T022604.707500_0000/` (completed export), [archive manifest](archive.json) |
| OpenAI, Claude, Grok and DeepSeek extension | [Full-run manifest](../../configs/closeout-full-v1.json), [route manifest](../../configs/closeout-routes-v1.json) | `experiments/closeout-full-v1/` (Initial campaign), `experiments/resilient-closeout-v2/` (resilient continuation), `experiments/closeout-full-v1/model-restarts/` (restart registrations) |
| Gemini native harness | [Gemini manifest](../../configs/gemini-full-v1.json) | `experiments/gemini-full-v1/` (Initial campaign), `experiments/resilient-closeout-v2/` (shared continuation), `experiments/sol-argon-restart-v1/` (Argon/Sol restart) |
| Argon concurrency increase | [16-worker manifest](../../configs/argon-speed-v1.json) | `experiments/argon-speed-v1/` in the archive |
| Final recovery calls | [Claude quota restart](../../configs/claude-quota-restart-v1.json), [DeepSeek single replacement](../../configs/deepseek-final-replacement-v1.json) | `experiments/claude-quota-restart-v1/` (Claude evidence), `experiments/deepseek-final-replacement-v1/` (DeepSeek evidence) |

For an independent live update run, use the Python 3.11 environment and pinned Inspect checkout in the historical guide. Sign in through `python -m study.auth` for ChatGPT, through Claude Code for Claude Max, and through Antigravity for Google. Only the paid Grok/DeepSeek routes need an OpenRouter key, configured with `python -m study.credentials set openrouter`; the subscription routes have no paid API-key fallback.

The campaign entry points below record the actual sequence; the archive contains the original bootstrap and one-off recovery utilities. **They use frozen campaign IDs and directories, so a new experiment needs new IDs, output directories and manifests before execution. Do not overwrite the completed campaign or reuse its one-off recovery scripts as a fresh run.**

| Stage | Prepare / inspect | Execute after setup and review |
| --- | --- | --- |
| First Sol/Astra cohort | `python -m study.expansion_run --per-condition 1` | `python -m study.expansion_run --per-condition 1 --execute`, then `--per-condition 100 --execute` |
| Extension smoke calls | `python scripts/closeout-smoke.py`; `python scripts/closeout-route-smoke.py` | Add `--execute` to each smoke command |
| Eleven-model extension | `python scripts/closeout-full.py` | `python scripts/closeout-full.py --execute` |
| Gemini full runs after validated native smokes | `python scripts/gemini-full.py` | `python scripts/gemini-full.py --execute` |

Match the per-model prompt variant, effort, provider route and judge visibility in the manifests. OpenAI keeps the optional scratchpad wording; later OpenAI runs request native summaries. Claude uses `prod=True` to remove the scratchpad paragraph and requests native summarized thinking. Gemini uses the recorded Antigravity harness. Primary judging uses visible responses, not provider-native reasoning. Matching successful smokes count toward the 100 trials per condition; failed attempts and labelled replacements remain in the ledger. See the [retry policy](../../docs/closeout-retry-policy-2026-11-02.txt).

Requests, raw provider/native streams, terminal receipts, extracted responses and scores are retained under the directories above. The complete archive includes these campaign records, failed attempts, labelled replacements, source snapshots and the exported ledger.
