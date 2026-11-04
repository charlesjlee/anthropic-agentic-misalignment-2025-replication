# Historical reproduction

Four models, six conditions, 100 responses per cell: **2,400 subjects and 2,400 completed Astra judgments**. See [Appendix F](../../README.md#appendix-f-reproduce-datacenter-scenario-with-historical-models) for interpretation and [results and run logs](results.md) for the evidence.

## Rebuild the analysis

First extract the historical release archive into the repository root as described in the [release guide](../../release/README.md). Then, from the repository root, with Python 3.11 and the saved evidence in `evidence/`:

```powershell
python experiments/historical/run.py verify
python experiments/historical/run.py compare
python experiments/historical/diagnose_scoring.py
```

These commands need no credentials or model calls. They verify the archive and rebuild the comparison and diagnostic tables without changing the root README. Download the compressed evidence from the [release assets](../../release/README.md).

## Set up a new run

Unit tests run with `uv run pytest`; they do not need credentials, evidence archives or the external reference checkout. Live source-parity checks require the pinned checkout below.

Use a fresh clone on Windows with Python 3.11 and [uv](https://docs.astral.sh/uv/getting-started/installation/). The current credential store uses Windows user-scoped encryption.

```powershell
uv sync --locked
git clone https://github.com/charlesjlee/inspect_evals.git external/inspect_evals
git -C external/inspect_evals checkout 4d5a56bb16a98633a9011674251e4ddcbd49ce9f
git clone https://github.com/anthropic-experimental/agentic-misalignment.git .local/reference/anthropic
git -C .local/reference/anthropic checkout ad2d8081dfd5ae612f16f5a431f4b6ee489815e7
.\.venv\Scripts\python.exe -m study.historical_audit
.\.venv\Scripts\Activate.ps1
python -m study.credentials set openai
python -m study.credentials set openrouter
python -m study.auth
```

Enter project API keys at the hidden prompts; they are encrypted under ignored `.local/auth/`. The runner does not use environment-variable keys. Fund OpenAI/OpenRouter usage and complete the ChatGPT sign-in for Astra, with paid fallback disabled. See [environment troubleshooting](../../docs/environment.md) if setup fails.

| Model | Route |
| --- | --- |
| `gpt-4.1-2025-04-14` | OpenAI |
| `gpt-4o-2024-08-06` | OpenAI |
| `deepseek/deepseek-r1-0528` | OpenRouter / `siliconflow/fp8` |
| `meta-llama/llama-4-maverick` | OpenRouter / `parasail/fp8` |
| `gpt-6-astra` judge | Sign-in with ChatGPT |

The [run configuration](../../configs/historical-run-plan.json) pins the subjects, routes, temperature 1, and 10,000-token cap. Endpoint availability must be checked again; changing a route or model changes the reproduction.

## Validate, then execute

The following commands consume API credits and ChatGPT allowance. Review the validation outputs before starting production:

```powershell
python -m study.api_validation --live
python -m study.openrouter_smoke --live
python experiments/historical/run.py preflight --live --directory evidence/validations/my-preflight
python experiments/historical/run.py run --live --preflight evidence/validations/my-preflight --directory evidence/historical/my-run
```

The runner checkpoints requests, responses, judgments, and scores. An interrupted run resumes from its existing directory:

```powershell
python experiments/historical/run.py resume --live --directory evidence/historical/my-run
```

Resolve failed or uncertain calls before resuming; reuse completed subject responses. The completed historical phase cannot be rerun in this checkout; use a separate clone, not a deleted registry. Commit deliberate source/configuration changes before execution. See [protocol and recovery details](../../docs/campaign-protocol.md) when needed.
