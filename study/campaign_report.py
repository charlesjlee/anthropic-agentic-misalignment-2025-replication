"""Offline completeness audit and human-review packet for a stopped/completed run."""

import argparse
import hashlib
import json
import math
from pathlib import Path

from inspect_evals.agentic_misalignment.classifiers.murder_classifier import (
    MurderClassifier,
)

from study import siwc
from study.api_validation import JUDGE, score_response
from study.campaign import load_inputs, trial_paths
from study.export_evidence import verify
from study.openrouter_smoke import PROVIDERS
from study.pilot import read, save


def interval(positive, n):
    if not n:
        return None
    z = 1.959963984540054
    p = positive / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return [max(0, center - radius), min(1, center + radius)]


def report(directory):
    status = read(directory / "status.json")
    if status["status"] in {"running", "draining_after_error", "prepared"}:
        raise ValueError("Do not finalize an active run")
    verify(directory)
    manifest, jobs, pairs, _ = load_inputs(directory)
    aggregates = {}
    for job in jobs:
        cell = aggregates.setdefault(
            job["pair"],
            {
                "model": job["model"],
                "condition": job["condition"],
                "planned": 0,
                "attempted": 0,
                "scored": 0,
                "intent_yes": 0,
                "original_action": 0,
                "original_harmful": 0,
            },
        )
        cell["planned"] += 1
        trial = directory / "trials" / job["id"]
        subject_path, result_path = trial_paths(trial)
        if not subject_path.exists():
            continue
        subject = read(subject_path)
        if subject["request"] != pairs[job["pair"]]["request"]:
            raise ValueError("Submitted request differs from frozen prompt")
        cell["attempted"] += 1
        if not result_path.exists():
            continue
        row = read(result_path)
        if row["status"] != "scored":
            continue
        judge = read(trial / "judge.json")
        if (
            subject["status"] != "completed"
            or subject["response"]["model"] != job["model"]
            or subject["response"]["choices"][0]["finish_reason"] != "stop"
            or judge["status"] != "completed"
            or judge["response"]["status"] != "completed"
            or judge["response"]["model"] != JUDGE
        ):
            raise ValueError("Incomplete result or substituted model")
        if (
            job["route"] == "openrouter"
            and subject["response"]["provider"]
            != PROVIDERS[subject["request"]["provider"]["only"][0]]
        ):
            raise ValueError("Substituted OpenRouter provider")
        expected, _ = siwc.prepare_request(
            JUDGE,
            MurderClassifier().get_grader_input(
                pairs[job["pair"]]["email"], row["subject_text"]
            ),
        )
        if (
            expected != judge["request"]
            or siwc.output_text(judge["response"]) != row["judge_text"]
        ):
            raise ValueError("Judge request/response mismatch")
        if (
            subject["response"]["choices"][0]["message"]["content"]
            != row["subject_text"]
        ):
            raise ValueError("Subject output mismatch")
        scores = score_response(row["subject_text"], row["judge_text"])
        if scores["original"] != row["scores"]["original"]:
            raise ValueError("Saved scores do not reproduce")
        cell["scored"] += 1
        cell["intent_yes"] += scores["original"]["intent"]
        cell["original_action"] += scores["original"]["action_gate"]
        cell["original_harmful"] += scores["original"]["harmful"]
    for cell in aggregates.values():
        cell["original_rate"] = (
            cell["original_harmful"] / cell["scored"] if cell["scored"] else None
        )
        cell["original_wilson_95"] = interval(cell["original_harmful"], cell["scored"])
    report = {
        "status": status,
        "kind": manifest["kind"],
        "cells": aggregates,
        "completeness": "complete" if status["scored"] == len(jobs) else "partial",
        "integrity": "Index and exact subject/judge request/output/score checks passed",
        "human_adjudication": "pending",
    }
    analysis = directory / "analysis"
    save(analysis / "report.json", report)
    lines = [
        "# Historical campaign report",
        "",
        f"Stage: {manifest['kind']}. Status: {status['status']}.",
        "",
        f"Planned {len(jobs)}; attempted {status['attempted']}; scored {status['scored']}. Human adjudication remains pending.",
        "",
        "| Model | Condition | Scored/planned | Harmful |",
        "| --- | --- | ---: | ---: |",
    ]
    for cell in aggregates.values():
        lines.append(
            f"| {cell['model']} | {cell['condition']['name']} | {cell['scored']}/{cell['planned']} | {cell['original_harmful']}/{cell['scored']} |"
        )
    lines += [
        "",
        "Validation observations are excluded from production estimates. Missing results are not harmless outcomes.",
        "",
        "Costs (reported OpenRouter / estimated OpenAI, USD): "
        + json.dumps(status["cost_usd_reported_or_estimated"]),
        "",
        "Raw evidence: ../index.json. Exact inputs: ../prompts.json. Outputs: ../trials/. Readable collection: ../transcripts.md.",
    ]
    (analysis / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    packet, key = [], []
    for item in read(directory / "review-queue.json"):
        row = read(trial_paths(directory / "trials" / item["sample_id"])[1])
        blind_id = hashlib.sha256(
            (directory.name + item["sample_id"]).encode()
        ).hexdigest()[:16]
        packet.append(
            {
                "review_id": blind_id,
                "messages": pairs[row["job"]["pair"]]["request"]["messages"],
                "subject_output": row["subject_text"],
                "human_intent": None,
                "human_action": None,
                "human_final": None,
                "notes": "",
            }
        )
        key.append(
            {
                "review_id": blind_id,
                **item,
                "model": row["job"]["model"],
                "scores": {"original": row["scores"]["original"]},
                "judge_output": row["judge_text"],
            }
        )
    packet.sort(key=lambda row: row["review_id"])
    save(analysis / "blinded-human-review.json", packet)
    save(analysis / "review-key-open-after-review.json", key)
    save(
        analysis / "index.json",
        {
            "files": [
                {
                    "path": p.name,
                    "bytes": p.stat().st_size,
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                }
                for p in sorted(analysis.iterdir())
                if p.is_file() and p.name != "index.json"
            ]
        },
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    result = report(parser.parse_args().directory.resolve())
    print(
        "Offline report:",
        result["completeness"],
        result["status"]["scored"],
        "scored; human review pending",
    )
