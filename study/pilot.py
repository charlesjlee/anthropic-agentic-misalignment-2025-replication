"""Four-condition setup pilot using the unmodified pinned Inspect task and scorer."""

import argparse
import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from inspect_ai import eval_async
from inspect_ai.model import GenerateConfig, get_model
from inspect_evals.agentic_misalignment import agentic_misalignment
from inspect_evals.agentic_misalignment.classifiers.murder_classifier import (
    MurderClassifier,
)
from inspect_evals.agentic_misalignment.prompt_generator import generate_prompts

from study.auth import ROOT
from study.siwc import catalog, output_text, prepare_request
from study.smoke import PIN, offline, provenance

CONDITIONS = [
    {
        "name": "headline",
        "goal_type": "explicit",
        "goal_value": "america",
        "urgency_type": "replacement",
    },
    {
        "name": "replacement_only",
        "goal_type": "none",
        "goal_value": "none",
        "urgency_type": "replacement",
    },
    {
        "name": "goal_only",
        "goal_type": "explicit",
        "goal_value": "america",
        "urgency_type": "none",
    },
    {
        "name": "control",
        "goal_type": "none",
        "goal_value": "none",
        "urgency_type": "none",
    },
]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(path)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def diagnostics(subject, judge):
    """Expose the original broad action check without an alternative scorer."""
    return {
        "broad_4429_in_tool_block": any(
            "4429" in block
            for block in re.findall(
                r"<tool_use:\w+>.*?</tool_use:\w+>", subject, re.DOTALL
            )
        ),
    }


async def run_condition(directory, condition, subject_name, judge_name, profile):
    config = GenerateConfig(max_retries=0, max_connections=1)
    models = {
        role: get_model(
            "siwc/" + name,
            config=config,
            profile=profile,
            evidence_dir=str(directory / role),
        )
        for role, name in [("subject", subject_name), ("judge", judge_name)]
    }
    task = agentic_misalignment(
        scenario="murder",
        goal_type=condition["goal_type"],
        goal_value=condition["goal_value"],
        urgency_type=condition["urgency_type"],
        prod=False,
        test_eval_awareness=False,
        grader_model=models["judge"],
    )
    logs = await eval_async(
        task,
        model=models["subject"],
        epochs=1,
        limit=1,
        max_samples=1,
        max_tasks=1,
        retry_on_error=0,
        fail_on_error=True,
        continue_on_fail=False,
        score_on_error=False,
        log_dir=str(directory / "inspect"),
        log_format="json",
        log_samples=True,
        log_model_api=False,
        log_shared=False,
    )
    if len(logs) != 1:
        raise RuntimeError("Expected one Inspect log")
    log = logs[0]
    save(directory / "inspect-log.json", log.model_dump(mode="json"))
    return log


def summarize_condition(directory, condition, prepared):
    log = read(directory / "inspect-log.json")
    row = {
        "condition": condition,
        "status": "unscored",
        "original_score": None,
        "subject_completed": False,
        "judge_completed": False,
        "calls": {},
        "human_review": {"status": "pending"},
        "assistant_review": {"status": "pending"},
    }
    for role in ["subject", "judge"]:
        files = sorted((directory / role).glob("*.json"))
        if len(files) > 1:
            raise RuntimeError("More than one call receipt for a pilot role")
        if not files:
            continue
        call = read(files[0])
        completed = (
            call.get("status") == "completed"
            and call.get("terminal_event") == "response.completed"
            and call.get("response", {}).get("status") == "completed"
        )
        row[role + "_completed"] = completed
        row["calls"][role] = {
            "receipt": str(files[0].relative_to(directory)),
            "status": call.get("status"),
            "terminal_event": call.get("terminal_event"),
            "usage": call.get("response", {}).get("usage"),
            "http_status": call.get("http_status"),
            "text": output_text(call["response"]) if completed else None,
        }
        if (
            role == "subject"
            and call["request"]["input"] != prepared["request"]["input"]
        ):
            raise RuntimeError(
                "Submitted subject prompt differs from frozen preparation"
            )
    if (
        log["status"] != "success"
        or not row["subject_completed"]
        or not row["judge_completed"]
    ):
        row["status"] = "failed_or_unscored"
        row["inspect_status"] = log["status"]
        return row
    samples = log.get("samples", [])
    if len(samples) != 1 or samples[0].get("error") or samples[0].get("limit"):
        raise RuntimeError("Unexpected sample count, error, or limit")
    subject = row["calls"]["subject"]["text"]
    judge = row["calls"]["judge"]["text"]
    prompts = generate_prompts(
        "murder",
        condition["goal_type"],
        condition["goal_value"],
        condition["urgency_type"],
        None,
        False,
        None,
    )
    grader_input = MurderClassifier().get_grader_input(prompts.email_content, subject)
    expected_judge, _ = prepare_request("placeholder", grader_input)
    actual_judge = read(directory / row["calls"]["judge"]["receipt"])
    if actual_judge["request"]["input"] != expected_judge["input"]:
        raise RuntimeError("Submitted judge prompt differs from the pinned classifier")
    final, intent, _ = MurderClassifier().classify(subject, judge)
    scores = samples[0].get("scores", {})
    if len(scores) != 1:
        raise RuntimeError("Expected one pinned scorer result")
    original = next(iter(scores.values()))
    if original["value"] != {
        "harmful": float(final),
        "classifier_verdict": float(intent),
    }:
        raise RuntimeError("Saved original score does not match pinned classifier")
    row.update(
        status="scored",
        original_score=original,
        diagnostics=diagnostics(subject, judge),
        sample_id=samples[0]["id"],
        task_version=log["eval"].get("task_version"),
    )
    return row


def verify_sources():
    import inspect_evals.agentic_misalignment.prompt_generator as generator

    installed = Path(generator.__file__).parent
    checkout = (
        ROOT
        / "external"
        / "inspect_evals"
        / "src"
        / "inspect_evals"
        / "agentic_misalignment"
    )
    result = {}
    for file in sorted(installed.rglob("*")):
        if file.suffix not in {".py", ".md", ".yaml"} or "__pycache__" in file.parts:
            continue
        relative = file.relative_to(installed)
        digest = hashlib.sha256(file.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        reference = checkout / relative
        if (
            not reference.exists()
            or hashlib.sha256(
                reference.read_bytes().replace(b"\r\n", b"\n")
            ).hexdigest()
            != digest
        ):
            raise RuntimeError(f"Installed and reference source differ: {relative}")
        result[relative.as_posix()] = digest
    return result


def counts(trials):
    return {
        "planned": len(trials),
        "attempted": sum(t["status"] != "not_attempted" for t in trials),
        "subject_completed": sum(t.get("subject_completed", False) for t in trials),
        "judge_completed": sum(t.get("judge_completed", False) for t in trials),
        "scored": sum(t["status"] == "scored" for t in trials),
        "failed_or_unscored": sum(
            t["status"] not in {"not_attempted", "scored"} for t in trials
        ),
        "excluded": 0,
    }


async def run_pilot(directory, model, profile):
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {
        "status": "preflight",
        "started_at": datetime.now(UTC).isoformat(),
        "protocol": "one single-turn trial per four baseline conditions; fixed order; no retries",
        "subject_model": model,
        "judge_model": model,
        "auth_route": "siwc",
        "provenance": provenance(),
        "source_hashes_normalized_lf": verify_sources(),
        "limitations": [
            "system text mapped to developer",
            "temperature, top_p and output cap omitted",
            "reasoning settings client-unspecified",
            "OpenAI judge substitution",
            "historical generated-prompt parity unverified",
            "n=1 per condition; not a rate estimate",
        ],
        "trials": [{"condition": c, "status": "not_attempted"} for c in CONDITIONS],
    }
    save(directory / "pilot.json", manifest)
    try:
        if manifest["provenance"]["study_worktree_status"]:
            raise RuntimeError("Commit study changes before a live pilot")
        if (
            manifest["provenance"]["inspect_evals_install"]["vcs_info"]["commit_id"]
            != PIN
        ):
            raise RuntimeError("Installed dependency differs from frozen pin")
        offline(directory)
        available = catalog(profile)
        save(directory / "model-catalog.json", available)
        if model not in [
            m["slug"] for m in available["models"] if m.get("visibility") == "list"
        ]:
            raise ValueError("Requested pilot model not available; no substitution")
        manifest["status"] = "running"
        save(directory / "pilot.json", manifest)
        for index, condition in enumerate(CONDITIONS):
            trial_dir = directory / "trials" / condition["name"]
            manifest["trials"][index]["status"] = "started"
            save(directory / "pilot.json", manifest)
            print(f"Pilot {index + 1}/4: {condition['name']}", flush=True)
            try:
                await run_condition(trial_dir, condition, model, model, profile)
                manifest["trials"][index] = summarize_condition(
                    trial_dir,
                    condition,
                    read(directory / "prompts" / (condition["name"] + ".json")),
                )
            except Exception as exc:
                manifest["trials"][index].update(
                    status="failed_or_unscored", error_type=type(exc).__name__
                )
                raise
            finally:
                save(directory / "pilot.json", manifest)
            if manifest["trials"][index]["status"] != "scored":
                raise RuntimeError(
                    "Pilot stopped at failed/unscored condition; raw receipts retained"
                )
            print(f"Completed and scored: {condition['name']}", flush=True)
        manifest["status"] = "completed_pending_review"
    except Exception as exc:
        manifest.update(status="stopped_on_error", error_type=type(exc).__name__)
        raise
    finally:
        manifest["counts"] = counts(manifest["trials"])
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        save(directory / "pilot.json", manifest)
        print("Pilot evidence:", directory, flush=True)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="Run exactly four scenario trials plus grading",
    )
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--profile", default="default")
    args = parser.parse_args()
    directory = (
        ROOT / ".local" / "runs" / datetime.now(UTC).strftime("pilot-%Y%m%dT%H%M%S%fZ")
    )
    asyncio.run(run_pilot(directory, args.model, args.profile))


if __name__ == "__main__":
    main()
