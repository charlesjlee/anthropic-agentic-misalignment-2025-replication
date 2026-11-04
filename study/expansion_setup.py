"""Offline bootstrap, historical import and smoke-inclusive configuration freezing."""

import hashlib
import json
from pathlib import Path

from study.auth import ROOT
from study.expansion_ledger import immutable_json

FROZEN = ROOT / "configs/expansion-smoke-v1.json"
PRICE_FILE = ROOT / "configs/expansion-prices-2026-11-01.json"


def read(path):
    return json.loads(Path(path).read_text("utf-8-sig"))


def artifact(path):
    return {
        "path": Path(path).relative_to(ROOT).as_posix(),
        "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
    }


def freeze():
    if FROZEN.exists():
        return read(FROZEN)
    from study.api_validation import request_for
    from study.historical_audit import CONDITIONS
    from study.pilot import verify_sources
    from study.smoke import provenance

    prompts = {}
    for condition in CONDITIONS:
        r, email = request_for("gpt-4.1-2025-04-14", condition)
        prompts[condition["name"]] = {"messages": r["messages"], "email": email}
    source = {
        p.relative_to(ROOT).as_posix(): p.read_text("utf-8")
        for p in sorted((ROOT / "study").glob("*.py"))
    }
    source_path = ROOT / ".local/expansion/frozen-source.json"
    immutable_json(source_path, source)
    body = {
        "schema": 1,
        "campaign": "openai-expansion-v1",
        "models": ["gpt-6.1-sol", "gpt-6-astra"],
        "actor_reasoning": {"effort": "medium", "mode": "standard"},
        "judge": {
            "model": "gpt-6-astra",
            "auth": "siwc",
            "reasoning": "omitted",
            "role_mapping": "system_to_developer",
        },
        "target_per_condition": 100,
        "smoke_per_condition": 1,
        "calibration_per_condition": 5,
        "include_smoke": True,
        "automatic_retries": 0,
        "paid_fallback": False,
        "schedule": "sample then condition then model; deterministic, no outcome-dependent selection",
        "prompts": prompts,
        "source_hashes": verify_sources(),
        "provenance": provenance(),
        "runner_sources": artifact(source_path),
        "protocol": "original deliberate-harm AND broad 4429 tool-use gate",
        "omitted_actor_controls": ["temperature", "top_p", "max_output_tokens"],
        "stopping": "Technical failure pauses; no outcome-dependent early stopping; provider-side spending limits",
    }
    immutable_json(FROZEN, body)
    return body
