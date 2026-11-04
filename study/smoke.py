"""Offline prompt checks and a two-call neutral SIWC smoke test. No scenario inference."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import platform
import subprocess
from datetime import UTC, datetime

from inspect_ai.model import (
    ChatMessageSystem,
    ChatMessageUser,
    GenerateConfig,
    get_model,
)

from study.auth import ROOT
from study.siwc import catalog, prepare_request  # Also registers the siwc provider.

PIN = "4d5a56bb16a98633a9011674251e4ddcbd49ce9f"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def provenance():
    return {
        "python": platform.python_version(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ["inspect-ai", "inspect-evals", "httpx", "PyJWT", "uv"]
        },
        "inspect_evals_commit": PIN,
        "inspect_evals_install": json.loads(
            importlib.metadata.distribution("inspect-evals").read_text(
                "direct_url.json"
            )
        ),
        "study_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "study_worktree_status": subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True
        ),
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
    }


def offline(directory):
    from inspect_evals.agentic_misalignment.prompt_generator import generate_prompts

    results = []
    for name, goal, value, urgency in [
        ("headline", "explicit", "america", "replacement"),
        ("replacement_only", "none", "none", "replacement"),
        ("goal_only", "explicit", "america", "none"),
        ("control", "none", "none", "none"),
    ]:
        prompts = generate_prompts("murder", goal, value, urgency, None, False, None)
        user_text = f"{prompts.user_prompt}\n{prompts.email_content}"
        request, mapping = prepare_request(
            "offline-placeholder",
            [
                ChatMessageSystem(content=prompts.system_prompt),
                ChatMessageUser(content=user_text),
            ],
        )
        assert request["input"][0]["content"] == prompts.system_prompt
        assert request["input"][1]["content"] == user_text
        write_json(
            directory / "prompts" / (name + ".json"),
            {"condition": name, "request": request, "mapping": mapping},
        )
        results.append(
            {
                "condition": name,
                "system_sha256": mapping[0]["sha256"],
                "user_sha256": mapping[1]["sha256"],
            }
        )
    report = {
        "status": "passed",
        "scope": "pinned-port prompt construction and SIWC text preservation; not historical prompt parity",
        "inference_calls": 0,
        "conditions": results,
        "provenance": provenance(),
    }
    write_json(directory / "offline-report.json", report)
    return report


async def live(directory, model_id, profile):
    available = catalog(profile)
    listed = [m["slug"] for m in available["models"] if m.get("visibility") == "list"]
    if model_id not in listed:
        raise ValueError("Requested model is not in this account's visible catalog")
    write_json(directory / "model-catalog.json", available)
    model = get_model(
        "siwc/" + model_id,
        config=GenerateConfig(max_retries=0, max_connections=1),
        profile=profile,
        evidence_dir=str(directory / "calls"),
    )
    report = {
        "status": "started",
        "purpose": "neutral transport and judge smoke test",
        "scenario_trials": 0,
        "model": model_id,
        "auth_route": "siwc",
        "provenance": provenance(),
        "calls": [],
    }
    try:
        subject = await model.generate(
            [
                ChatMessageSystem(content="Follow the user's instruction precisely."),
                ChatMessageUser(content="Reply with exactly: BLUE"),
            ]
        )
        report["calls"].append(
            {"role": "subject", "output": subject.model_dump(mode="json")}
        )
        write_json(directory / "smoke-report.json", report)
        judge = await model.generate(
            [
                ChatMessageSystem(
                    content="You grade a neutral exact-match test. Reply only PASS if the supplied response is exactly BLUE; otherwise reply FAIL."
                ),
                ChatMessageUser(content=json.dumps({"response": subject.completion})),
            ]
        )
        report["calls"].append(
            {"role": "judge", "output": judge.model_dump(mode="json")}
        )
        report["passed"] = subject.completion == "BLUE" and judge.completion == "PASS"
        report["status"] = "passed" if report["passed"] else "failed_assertion"
        if not report["passed"]:
            raise RuntimeError("Neutral smoke output did not match expected results")
    except Exception as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        write_json(directory / "smoke-report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="Authorize two neutral calls; never runs scenario trials",
    )
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--profile", default="default")
    args = parser.parse_args()
    directory = (
        ROOT / ".local" / "runs" / datetime.now(UTC).strftime("setup-%Y%m%dT%H%M%S%fZ")
    )
    offline(directory)
    print("Offline prompt preparation passed for four conditions.", flush=True)
    if args.live:
        report = asyncio.run(live(directory, args.model, args.profile))
        print("Neutral subject/judge smoke:", report["status"], flush=True)
    print("Evidence:", directory, flush=True)


if __name__ == "__main__":
    main()
