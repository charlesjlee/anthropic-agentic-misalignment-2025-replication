"""Offline update reproduction: export the ledger, verify evidence, replay scores.

No command in this entry point makes model calls or reads authentication secrets.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def checked_path(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes evidence root: {name}")
    return path


def checked_artifact(root, item):
    path = checked_path(root, item["path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
        raise ValueError(f"Artifact changed: {item['path']}")
    return path


def extract(root, event):
    body = event["body"]
    path = checked_artifact(root, body["artifact"])
    receipt = read(path)
    if body["status"] != "completed" or receipt["status"] not in {
        "completed",
        "completed_native_smoke",
    }:
        raise ValueError(
            f"Incomplete call cannot be scored: {event['id']} {body['status']} {receipt['status']}"
        )
    if body.get("request_sha256") and body["auth"] in {"siwc", "openrouter"}:
        from study.expansion_ledger import digest

        if digest(receipt["request"]) != body["request_sha256"]:
            raise ValueError("Request hash mismatch")
    auth = body["auth"]
    if auth in {"claude_subscription", "gemini_subscription"}:
        text = receipt["visible_text"]
        if receipt.get("native_tools_executed"):
            raise ValueError("Unexpected native tool execution")
    elif auth == "openrouter":
        choice = receipt["response"]["choices"][0]
        if choice["finish_reason"] != "stop":
            raise ValueError("Incomplete OpenRouter response")
        text = choice["message"]["content"]
    elif auth == "siwc":
        from study.siwc import output_text

        terminal = receipt["response"]
        if receipt["terminal_event"] != "response.completed":
            raise ValueError("Missing terminal completion")
        events = [
            json.loads(s)
            for s in (path.parent / "stream.jsonl").read_text("utf-8").splitlines()
        ]
        completed = [
            e["response"] for e in events if e.get("type") == "response.completed"
        ]
        if completed != [terminal] or terminal["status"] != "completed":
            raise ValueError("Stream and receipt disagree")
        if terminal.get("output"):
            response = terminal
        else:
            items = {}
            for e in events:
                if e.get("type") == "response.output_item.done":
                    if e["output_index"] in items:
                        raise ValueError("Duplicate completed output item")
                    items[e["output_index"]] = e["item"]
            if not items or sorted(items) != list(range(len(items))):
                raise ValueError("Missing output items")
            response = {**terminal, "output": [items[i] for i in sorted(items)]}
        text = output_text(response)
    else:
        raise ValueError(f"Unsupported auth route: {auth}")
    if not text.strip():
        raise ValueError("Empty scored output")
    extracted = path.parent / "extracted.json"
    if extracted.exists() and read(extracted)["visible_text"] != text:
        raise ValueError("Extracted text differs from provider receipt")
    return text


def replay(root, snapshot):
    from study.api_validation import score_response

    configs = {c["id"]: c for c in snapshot["configs"]}
    events = snapshot["events"]
    event_map = {e["id"]: e for e in events}
    calls = defaultdict(list)
    for event in events:
        if event["kind"] == "call" and event["body"]["status"] == "completed":
            calls[event["config"], event["trial"], event["body"]["role"]].append(event)
    records, counts, seen = [], Counter(), set()
    for event in events:
        if event["kind"] != "score":
            continue
        cid, tid, score = event["config"], event["trial"], event["body"]
        if (cid, tid) in seen or tid != f"{score['condition']}-{score['sample']:03d}":
            raise ValueError("Duplicate or misidentified trial")
        seen.add((cid, tid))
        if not 1 <= score["sample"] <= 100:
            raise ValueError("Invalid sample number")
        visible, selected = {}, {}
        for role in ("actor", "judge"):
            named = score.get("calls", score.get("call_events", {})).get(role)
            candidates = calls[cid, tid, role]
            if named:
                call = event_map[named]
            elif role in score:
                matches = [
                    e for e in candidates if e["body"]["artifact"] == score[role]
                ]
                if len(matches) != 1:
                    raise ValueError("Ambiguous receipt link")
                call = matches[0]
            else:
                if len(candidates) != 1:
                    raise ValueError(f"Ambiguous successful call: {cid}/{tid}/{role}")
                call = candidates[0]
            if (call["config"], call["trial"], call["body"]["role"]) != (
                cid,
                tid,
                role,
            ):
                raise ValueError("Cross-trial call reference")
            visible[role] = extract(root, call)
            selected[role] = call["body"]["artifact"]
        actual = score_response(visible["actor"], visible["judge"])["original"]
        if actual != score["scores"]:
            raise ValueError(f"Score replay differs: {cid}/{tid}")
        counts[cid, score["condition"]] += 1
        records.append(
            {
                "config": cid,
                "model": configs[cid]["model"],
                "trial": tid,
                "condition": score["condition"],
                "sample": score["sample"],
                "scores": actual,
                "receipts": selected,
            }
        )
    condition_set = {
        "explicit_replacement",
        "explicit_restriction",
        "explicit_none",
        "none_replacement",
        "none_restriction",
        "none_none",
    }
    completed_ids = {r["config"] for r in records}
    if len(records) != 9000 or len(completed_ids) != 15:
        raise ValueError(
            f"Expected 15 models / 9000 trials, found {len(completed_ids)} / {len(records)}"
        )
    if any(
        counts[cid, condition] != 100
        for cid in completed_ids
        for condition in condition_set
    ):
        raise ValueError("Incomplete model-condition cell")
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", nargs="?", default="verify", choices=["export", "verify"]
    )
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="Repository or extracted archive root"
    )
    parser.add_argument(
        "--archive", type=Path, help="Verify a release ZIP in isolation"
    )
    args = parser.parse_args()
    if args.archive:
        if args.action != "verify":
            parser.error("--archive is only valid with verify")
        with tempfile.TemporaryDirectory(prefix="datacenter-verify-") as temporary:
            directory = Path(temporary)
            with zipfile.ZipFile(args.archive) as archive:
                for member in archive.infolist():
                    checked_path(directory, member.filename)
                archive.extractall(directory)
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "verify",
                    "--root",
                    str(directory),
                ],
                check=True,
            )
        return
    root = args.root.resolve()
    if args.action == "export":
        from study.expansion_ledger import Ledger, digest

        configs, events = Ledger().snapshot()
        configs = [c for c in configs if not c["id"].startswith("historical/")]
        selected = {c["id"] for c in configs}
        events = [e for e in events if e["config"] in selected]
        snapshot = {"configs": configs, "events": events}
        records = replay(root, snapshot)
        (HERE / "ledger-snapshot.json").write_text(
            json.dumps(snapshot, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (HERE / "trials.json").write_text(
            json.dumps(records, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        summary = {
            "models": 15,
            "trials": len(records),
            "harmful": sum(r["scores"]["harmful"] for r in records),
            "score_replay": "exact",
            "snapshot_sha256": digest(snapshot),
            "failed_calls_retained": sum(
                e["kind"] == "call" and e["body"]["status"] != "completed"
                for e in events
            ),
            "scope": "Original automated scorer; not an independent human adjudication. Native reasoning excluded from primary judging.",
        }
        (HERE / "summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
    else:
        directory = root / "experiments/update"
        index = read(directory / "archive-index.json")
        for item in index["files"]:
            checked_artifact(root, item)
        snapshot = read(directory / "ledger-snapshot.json")
        records = replay(root, snapshot)
        if records != read(directory / "trials.json"):
            raise ValueError("Trial export differs")
        from study.expansion_ledger import digest

        if digest(snapshot) != read(directory / "summary.json")["snapshot_sha256"]:
            raise ValueError("Snapshot digest differs")
        summary = {
            "verified_files": len(index["files"]),
            "replayed_trials": len(records),
            "harmful": sum(r["scores"]["harmful"] for r in records),
        }
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
