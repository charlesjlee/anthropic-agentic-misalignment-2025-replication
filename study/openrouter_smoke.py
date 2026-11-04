"""Exactly one headline-condition request per fixed OpenRouter route; no campaign."""

import argparse
import time
from datetime import UTC, datetime

import httpx

from study.api_validation import INPUT_BOUND, judge_call, request_for
from study.auth import ROOT
from study.credentials import read_key
from study.historical_audit import CONDITIONS
from study.pilot import read, save
from study.smoke import provenance

BASE = "https://openrouter.ai/api/v1"
PROVIDERS = {"siliconflow/fp8": "SiliconFlow", "parasail/fp8": "Parasail"}
CEILING_USD = 0.05


def checked_completion(response, model, provider):
    if response.get("model") != model or response.get("provider") != provider:
        raise ValueError("Unexpected model/provider; no substitute accepted")
    choices = response.get("choices", [])
    if len(choices) != 1 or choices[0].get("finish_reason") != "stop":
        raise ValueError("Incomplete or unexpected completion")
    message = choices[0]["message"]
    if (
        not isinstance(message.get("content"), str)
        or not message["content"].strip()
        or message.get("tool_calls")
    ):
        raise ValueError("No completed text response")
    usage = response.get("usage", {})
    if any(
        not isinstance(usage.get(k), int) or usage[k] < 0
        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
    ):
        raise ValueError("Missing token usage")
    if usage["prompt_tokens"] > INPUT_BOUND or usage["completion_tokens"] > 10000:
        raise ValueError("Usage exceeds smoke budget assumptions")
    return message["content"]


def budget_for(request):
    if request["max_tokens"] != 10000 or request["temperature"] != 1:
        raise ValueError("Unexpected historical controls")
    if (
        sum(len(m["content"].encode()) for m in request["messages"]) + 1024
        > INPUT_BOUND
    ):
        raise ValueError("Input exceeds budget bound")
    p = request["provider"]
    if (
        p["allow_fallbacks"]
        or not p["require_parameters"]
        or len(p["only"]) != 1
        or p["only"][0] not in PROVIDERS
    ):
        raise ValueError("Route is not pinned")
    return (
        INPUT_BOUND * p["max_price"]["prompt"] + 10000 * p["max_price"]["completion"]
    ) / 1e6


def account_snapshot(key):
    result = {}
    for route, fields in [
        ("credits", ("total_credits", "total_usage")),
        ("key", ("limit", "limit_remaining", "limit_reset", "usage", "is_free_tier")),
    ]:
        response = httpx.get(
            BASE + "/" + route, headers={"Authorization": "Bearer " + key}, timeout=30
        )
        result[route] = {"http_status": response.status_code}
        if response.status_code == 200:
            data = response.json()["data"]
            result[route].update({k: data.get(k) for k in fields})
    return result


def run(directory):
    directory.mkdir(parents=True, exist_ok=False)
    config = read(ROOT / "configs/openrouter-validation.json")
    manifest = {
        "status": "preflight",
        "started_at": time.time(),
        "provenance": provenance(),
        "purpose": "Two-call plumbing smoke, excluded from production; not six-condition validation",
        "paid_call_limit": 2,
        "ceiling_usd": CEILING_USD,
        "reserved_usd": 0,
        "trials": [],
    }
    save(directory / "smoke.json", manifest)
    try:
        if manifest["provenance"]["study_worktree_status"]:
            raise ValueError("Commit smoke code before live execution")
        key = read_key("openrouter")
        save(directory / "account-before.json", account_snapshot(key))
        seed_request, email = request_for("gpt-4.1-2025-04-14", CONDITIONS[0])
        planned = []
        for model in config["models"]:
            response = httpx.get(
                BASE + "/models/" + model["model"] + "/endpoints", timeout=30
            )
            response.raise_for_status()
            catalog = response.json()
            slug = model["provider"]["only"][0]
            endpoints = [e for e in catalog["data"]["endpoints"] if e["tag"] == slug]
            if len(endpoints) != 1:
                raise ValueError("Pinned endpoint absent")
            endpoint = endpoints[0]
            if endpoint["quantization"] != "fp8" or not {
                "temperature",
                "max_tokens",
            }.issubset(endpoint["supported_parameters"]):
                raise ValueError("Missing endpoint precision/control support")
            for field in ("prompt", "completion"):
                if (
                    float(endpoint["pricing"][field]) * 1e6
                    > model["provider"]["max_price"][field] + 1e-9
                ):
                    raise ValueError("Price increased above frozen ceiling")
            case = model["model"].replace("/", "__")
            save(directory / "catalogs" / (case + ".json"), catalog)
            request = {
                "model": model["model"],
                "messages": seed_request["messages"],
                "temperature": 1.0,
                "max_tokens": 10000,
                "provider": model["provider"],
                "transforms": [],
            }
            planned.append((case, request, budget_for(request)))
        if len(planned) != 2 or sum(p[2] for p in planned) > CEILING_USD:
            raise ValueError("Smoke budget exceeded")
        save(
            directory / "prepared.json", {case: request for case, request, _ in planned}
        )
        for case, request, reservation in planned:
            row = {
                "case_id": case,
                "condition": CONDITIONS[0],
                "status": "reserved_before_request",
                "reserved_usd": reservation,
            }
            manifest["trials"].append(row)
            manifest["reserved_usd"] += reservation
            save(directory / "smoke.json", manifest)
            receipt = {
                "request": request,
                "route": BASE + "/chat/completions",
                "started_at": time.time(),
                "status": "started",
            }
            path = directory / "trials" / case / "subject.json"
            save(path, receipt)
            print("OpenRouter smoke: " + request["model"], flush=True)
            try:
                response = httpx.post(
                    receipt["route"],
                    json=request,
                    headers={"Authorization": "Bearer " + key},
                    timeout=httpx.Timeout(300, connect=30),
                )
                receipt["http_status"] = response.status_code
                if response.status_code != 200:
                    raise RuntimeError("OpenRouter HTTP failure; no retry or fallback")
                receipt["response"] = response.json()
                subject = checked_completion(
                    receipt["response"],
                    request["model"],
                    PROVIDERS[request["provider"]["only"][0]],
                )
                receipt["status"] = "completed"
                usage = receipt["response"]["usage"]
                price = request["provider"]["max_price"]
                receipt["list_cost_estimate_usd"] = (
                    usage["prompt_tokens"] * price["prompt"]
                    + usage["completion_tokens"] * price["completion"]
                ) / 1e6
                row.update(status="subject_completed", subject=subject)
                save(path, receipt)
                save(directory / "smoke.json", manifest)
                judge, scores = judge_call(
                    directory / "trials" / case / "judge", email, subject
                )
                row.update(status="scored", judge=judge, scores=scores)
            except Exception as exc:  # noqa: BLE001 - persist failure type without secret-bearing error text
                row.update(status="failed_or_unscored", error_type=type(exc).__name__)
                receipt.setdefault("error_type", type(exc).__name__)
                # Independent second route still gets its one planned request; no retries.
            finally:
                receipt["finished_at"] = time.time()
                save(path, receipt)
                save(directory / "smoke.json", manifest)
        save(directory / "account-after.json", account_snapshot(key))
        manifest["status"] = (
            "completed"
            if all(r["status"] == "scored" for r in manifest["trials"])
            else "completed_with_failures"
        )
    except Exception as exc:  # noqa: BLE001 - do not leak authentication exception text
        manifest.update(status="stopped", error_type=type(exc).__name__)
    finally:
        manifest["finished_at"] = time.time()
        save(directory / "smoke.json", manifest)
        parts = ["# OpenRouter plumbing smoke; not production\n"]
        for row in manifest["trials"]:
            parts += [
                "## " + row["case_id"],
                "### Subject\n" + row.get("subject", "No complete output"),
                "### Judge\n" + row.get("judge", "No completed judge"),
            ]
        (directory / "transcripts.md").write_text("\n\n".join(parts), encoding="utf-8")
        print(
            "Smoke status: " + manifest["status"] + "; evidence: " + str(directory),
            flush=True,
        )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    directory = (
        ROOT
        / ".local/runs"
        / datetime.now(UTC).strftime("openrouter-smoke-%Y%m%dT%H%M%S%fZ")
    )
    result = run(directory)
    raise SystemExit(0 if result["status"] == "completed" else 1)
