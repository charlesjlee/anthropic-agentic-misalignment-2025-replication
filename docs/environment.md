# Local environment and neutral smoke test

Verified on Windows with Python 3.11.9, uv 0.13.6, and Inspect AI 0.3.286 on 2026-10-28. The dependency lock pins Inspect Evals to `4d5a56bb16a98633a9011674251e4ddcbd49ce9f`. The ignored checkout at `external/inspect_evals` is a clean reference checkout. The environment installs the pinned Git dependency, not an editable copy. No fork changes were needed.

Run commands from the repository root in ordinary PowerShell under the Windows account that completed sign-in. Credentials are encrypted for that account.

## Restore or check

The existing environment contains uv. For a fresh installation, install uv following its official instructions and substitute `uv` for the executable path below. Python must satisfy the project's 3.11 requirement. This Git long-path setting applies only to this process and its children.

```powershell
$env:GIT_CONFIG_COUNT='1'
$env:GIT_CONFIG_KEY_0='core.longpaths'
$env:GIT_CONFIG_VALUE_0='true'
.\.venv\Scripts\uv.exe --cache-dir .local/uv-cache sync --locked
.\.venv\Scripts\uv.exe --cache-dir .local/uv-cache run --locked pytest tests -q
.\.venv\Scripts\uv.exe --cache-dir .local/uv-cache run --locked ruff check study tests
.\.venv\Scripts\uv.exe --cache-dir .local/uv-cache run --locked ruff format --check study tests
```

The workspace-local cache is optional. Keep certificate verification enabled.

## Sign in or reauthorize

```powershell
.\.venv\Scripts\uv.exe --cache-dir .local/uv-cache run --locked python -m study.auth
```

This opens the system browser for Data Center Study. Review its ChatGPT-plan permission and keep optional paid-credit fallback disabled. Credentials are Windows DPAPI ciphertext under ignored `.local/auth/`, independent of Codex credentials. Keep this directory private. Separate registrations can use `--profile NAME` on auth and smoke commands. Reauthorization reuses that profile's client ID and the stable host ID. Refresh runs near expiry under a profile lock and atomically stores the replacement token set. Initial authorization was verified live; natural-expiry refresh has not yet been observed live.

## Offline preparation

```powershell
.\.venv\Scripts\uv.exe --cache-dir .local/uv-cache run --locked python -m study.smoke
```

This constructs four conditions locally and saves source text, request representation, and hashes under a new `.local/runs/setup-*` directory. It makes no inference calls. It verifies text preservation from the pinned port into SIWC, not parity with the historical Anthropic runner.

## Neutral smoke

```powershell
.\.venv\Scripts\uv.exe --cache-dir .local/uv-cache run --locked python -m study.smoke --live --model gpt-6.1-sol
```

This consumes ChatGPT-plan usage for two neutral requests through Inspect's `siwc` provider: a subject instructed to return `BLUE`, and a judge grading that answer. Both must complete and match the expected outputs. It never submits the prepared scenario prompts. A new evidence directory contains the model catalog, requests, exact role mapping, streamed events, terminal responses, usage, provenance, and report. Errors retain evidence and raise exceptions. No automatic model substitution, inference retry, or paid API fallback is configured.

The adapter supports text messages without executable tools. It rejects unsupported generation settings. Original system text maps to a developer message. Temperature and output-token caps remain unspecified by the client. The neutral judge does not validate the scenario harmfulness scorer.

## Completed study

Both the historical reproduction and newer-model update are complete; see the root README and reproduction guides.

Contracts: [SIWC registration](https://developers.openai.com/siwc/token-sharing-open-source/sign-in), [inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference), [limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations), and [refresh](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions).
