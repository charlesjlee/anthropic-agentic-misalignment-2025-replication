"""Frozen subscription trials with bounded concurrency; no automatic retries."""

import argparse
import hashlib
from pathlib import Path

from filelock import FileLock

from study.auth import ROOT
from study.expansion_ledger import (
    DEFAULT_DB,
    Ledger,
    digest,
    immutable_json,
    now,
    usage,
)
from study.expansion_setup import FROZEN, artifact, read
from study.expansion_transport import invoke, response_request


def jobs(frozen, count):
    if count not in {1, 5, 100}:
        raise ValueError("Wave must be 1, 5 or 100 samples per condition")
    for sample in range(1, count + 1):
        for condition in frozen["prompts"]:
            for model in frozen["models"]:
                yield model, condition, sample, f"{condition}-{sample:03d}"


def run(
    db=DEFAULT_DB, count=1, execute=False, workers=1, limit=None, exclude_trials=()
):
    if not 1 <= workers <= 64 or (limit is not None and limit < 1):
        raise ValueError("Invalid workers or trial limit")
    ledger = Ledger(db)
    frozen = read(FROZEN)
    configs, _events = ledger.snapshot()
    config_map = {c["id"]: c for c in configs}
    if not execute:
        return {
            "mode": "dry-run",
            "configuration_sha256": digest(frozen),
            "actor_slots_in_wave": len(list(jobs(frozen, count))),
            "includes_prior_slots": True,
            "target_per_model": 600,
            "paid_calls": 0,
            "workers": workers,
            "new_trial_limit": limit,
            "excluded_trials": list(exclude_trials),
        }
    from study.expansion_parallel import bounded_trials
    from study.pilot import verify_sources

    if verify_sources() != frozen["source_hashes"]:
        raise ValueError("Source hashes changed")
    # Frozen source snapshots preserve exact local serialization/scoring implementations.
    sources = read(ROOT / frozen["runner_sources"]["path"])
    amendment_paths = sorted(
        (ROOT / ".local/expansion").glob("execution-amendment-[0-9][0-9][0-9].json")
    )
    amended_hashes = {}
    amendment_path = amendment_paths[-1] if amendment_paths else None
    previous_path = None
    for path in amendment_paths:
        amendment = read(path)
        if amendment["manifest_sha256"] != digest(frozen):
            raise ValueError("Execution amendment manifest mismatch")
        if previous_path and amendment.get("previous_amendment") != artifact(
            previous_path
        ):
            raise ValueError("Execution amendment chain mismatch")
        amended_hashes.update(amendment["current_file_sha256"])
        previous_path = path
    for name, expected in amended_hashes.items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Execution amendment source mismatch: {name}")
    for name, text in sources.items():
        if (ROOT / name).read_text("utf-8") != text and (name not in amended_hashes):
            raise ValueError(f"Runner source drift: {name}; freeze a new version")
    with FileLock(str(Path(db).with_suffix(".runner.lock")), timeout=0):
        _, all_events = ledger.snapshot()
        scored = {e["id"] for e in all_events if e["kind"] == "score"}
        pending = [
            job
            for job in jobs(frozen, count)
            if f"expansion/{job[0]}/v1/{job[3]}/score" not in scored
            and f"{job[0]}/{job[3]}" not in exclude_trials
        ]
        if limit is not None:
            pending = pending[:limit]
        execution = {
            "started_at": now(),
            "workers": workers,
            "new_trial_limit": limit,
            "excluded_trials": list(exclude_trials),
            "per_condition": count,
            "scheduling": "bounded trial concurrency",
        }
        run_id = execution["started_at"].replace(":", "").replace("+", "_")
        record_dir = ROOT / ".local/expansion/executions" / run_id
        immutable_json(
            record_dir / "start.json",
            {
                **execution,
                "jobs": pending,
                "amendment": artifact(amendment_path) if amendment_path else None,
            },
        )
        result = bounded_trials(
            pending,
            lambda job: trial(
                ledger, frozen, config_map, amendment_path, execution, job
            ),
            workers,
            stopped=lambda: (ROOT / ".local/expansion/STOP").exists(),
        )
        result["status"] = (
            "paused_for_reconciliation"
            if result["errors"]
            else "paused_by_stop_file"
            if result["stopped"]
            else "bounded_wave_completed"
            if limit
            else "wave_completed"
        )
        result["samples_per_condition"] = count
        result["execution"] = execution
        immutable_json(record_dir / "result.json", result)
        return result


def trial(ledger, frozen, config_map, amendment_path, execution, job):
    from inspect_evals.agentic_misalignment.classifiers.murder_classifier import (
        MurderClassifier,
    )

    from study.api_validation import score_response
    from study.expansion_recovery import attempt_location
    from study.siwc import output_text

    model, condition, sample, trial = job
    config_id = f"expansion/{model}/v1"
    config = config_map[config_id]
    if config["configuration_sha256"] != digest(
        {"manifest": digest(frozen), "model": model, "auth": "siwc"}
    ):
        raise ValueError("Frozen configuration mismatch")
    _, all_events = ledger.snapshot()
    existing = {
        e["id"]: e
        for e in all_events
        if e["config"] == config_id and e["trial"] == trial
    }
    if f"{config_id}/{trial}/score" in existing:
        return {"status": "already_scored"}
    prompt = frozen["prompts"][condition]
    actor_text = None
    call_events = {}
    trial_directory = ROOT / ".local/expansion/runs" / model / trial
    for role in ["actor", "judge"]:
        messages = (
            prompt["messages"]
            if role == "actor"
            else [
                {"role": m.role, "content": m.content}
                for m in MurderClassifier().get_grader_input(
                    prompt["email"], actor_text
                )
            ]
        )
        request = response_request(
            model if role == "actor" else frozen["judge"]["model"],
            messages,
            "siwc",
            frozen["actor_reasoning"] if role == "actor" else None,
        )
        directory = trial_directory / role
        event_id = f"{config_id}/{trial}/{role}/call"
        directory, event_id, recovery = attempt_location(directory, event_id)
        call_events[role] = event_id
        if event_id in existing:
            saved_artifact = existing[event_id]["body"]["artifact"]
            if artifact(ROOT / saved_artifact["path"]) != saved_artifact:
                raise ValueError("Recovered receipt hash mismatch")
            receipt = read(ROOT / existing[event_id]["body"]["artifact"]["path"])
        else:
            # An existing receipt after a crash is recoverable without repurchasing.
            receipt_path = directory / "receipt.json"
            if receipt_path.exists():
                receipt = read(receipt_path)
            else:
                ledger.reserve(
                    config_id, trial, role + ("/retry-001" if recovery else ""), request
                )
                receipt = invoke(request, "siwc", directory)
            if receipt["request"] != request:
                raise ValueError("Recovered request differs")
            ledger.event(
                event_id,
                config_id,
                trial,
                "call",
                {
                    "role": role,
                    "status": receipt["status"],
                    "usage": usage(receipt.get("response") or {}),
                    "auth": "siwc",
                    "tier": "subscription",
                    "rates": config["rates"]
                    if role == "actor"
                    else config["judge_rates"],
                    "artifact": artifact(directory / "receipt.json"),
                    "condition": condition,
                    "sample": sample,
                    "phase": "smoke"
                    if sample == 1
                    else "calibration"
                    if sample <= 5
                    else "full",
                    "request_sha256": digest(request),
                    "configuration_sha256": digest(frozen),
                    "execution": execution,
                    **(recovery or {}),
                    "provider_refusal": any(
                        p.get("type") == "refusal"
                        for item in (receipt.get("response") or {}).get("output", [])
                        for p in item.get("content", [])
                    ),
                    "truncation": receipt["status"] == "incomplete",
                },
            )
        if receipt["request"] != request:
            raise ValueError("Saved request differs from frozen request")
        if receipt["status"] != "completed":
            raise RuntimeError(f"{role} {receipt['status']}; paused for reconciliation")
        from study.expansion_stream import assembled_response

        visible = assembled_response(receipt, directory)
        text = output_text(visible)
        if not text.strip():
            raise ValueError(
                "Empty visible output; requires review, never retry automatically"
            )
        if role == "actor":
            actor_text = text
        else:
            try:
                scores = score_response(actor_text, text)["original"]
            except ValueError:
                ledger.event(
                    f"{config_id}/{trial}/scoring-error",
                    config_id,
                    trial,
                    "scoring_error",
                    {"reason": "original classifier could not parse judge output"},
                )
                raise
            result = {
                "condition": condition,
                "sample": sample,
                "scores": scores,
                "judge_config": frozen["judge"],
                "execution": execution,
                "call_events": call_events,
                "execution_amendment": artifact(amendment_path)
                if amendment_path
                else None,
            }
            immutable_json(trial_directory / "score.json", result)
            ledger.event(
                f"{config_id}/{trial}/score", config_id, trial, "score", result
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--per-condition", type=int, choices=[1, 5, 100], default=1)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum pending trials to execute in this bounded wave",
    )
    parser.add_argument(
        "--exclude-trial",
        action="append",
        default=[],
        help="Hold an identified trial for explicit reconciliation; never treat it as complete",
    )
    args = parser.parse_args()
    print(
        run(
            args.db,
            args.per_condition,
            args.execute,
            args.workers,
            args.limit,
            args.exclude_trial,
        )
    )
