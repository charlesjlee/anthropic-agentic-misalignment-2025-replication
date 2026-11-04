"""Resume Argon at 16 workers using the existing quota-aware continuation."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "quota_continuation", ROOT / "scripts/restart-sol-argon.py"
)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
r = helper.r
PRIOR = ROOT / "experiments/sol-argon-restart-v1"
RUNS = ROOT / "experiments/argon-speed-v1"
MANIFEST = ROOT / "configs/argon-speed-v1.json"
MODEL = "gemini-4-argon-medium"


def prepare():
    original = r.read(ROOT / "configs/resilient-closeout-v2.json")
    for a in original["sources"]:
        if r.artifact(ROOT / a["path"]) != a:
            raise ValueError("Frozen source drift")
    prior = r.read(ROOT / "configs/sol-argon-restart-v1.json")
    if r.artifact(ROOT / "scripts/restart-sol-argon.py") != prior["runner"]:
        raise ValueError("Quota-aware continuation source drift")
    model = next(m for m in prior["models"] if m["model"] == MODEL)
    if hashlib.sha256(r.gemini.CLI.read_bytes()).hexdigest() != model["cli_sha256"]:
        raise ValueError("Gemini CLI binary drift")
    if r.verify_sources() != r.read(r.gemini.MANIFEST)["source_hashes"]:
        raise ValueError("Prompt/classifier drift")
    if MANIFEST.exists():
        f = r.read(MANIFEST)
        if r.artifact(Path(__file__)) != f["runner"]:
            raise ValueError("Speed runner source drift")
        return f
    f = {
        "created": r.now(),
        "authorization": "User requested speeding up the resumed Gemini 4 Argon run",
        "models": [{**model, "workers": 16}],
        "prior_manifest": r.artifact(ROOT / "configs/sol-argon-restart-v1.json"),
        "runner": r.artifact(Path(__file__)),
        "prior_workers": 4,
        "workers": 16,
        "baseline": {
            "window_start": "2026-11-03T19:28:18.757918+00:00",
            "scored": 22,
            "minutes": 3.97,
            "trials_per_minute": 5.54,
        },
        "inference_settings_changed": False,
        "policy": "Reuse completed trials; preserve failed receipts and labelled retries. Keep the existing bounded retry and native quota-reset waiting policy. No paid fallback. Retain the telemetry sharing-violation fix. Only Argon restarts; no Sol recovery or rerun.",
    }
    r.immutable_json(MANIFEST, f)
    for path in [Path(__file__), ROOT / "scripts/restart-sol-argon.py"]:
        dest = RUNS / "executed-source" / path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(path.read_bytes())
    return f


def main():
    f = prepare()
    model = f["models"][0]
    if r.read(PRIOR / "lanes" / (MODEL + ".json"))["state"] != "paused":
        raise RuntimeError("Prior Argon workers have not drained")
    _, events = r.Ledger().snapshot()
    recorded = {e["id"].rsplit("/", 1)[0] for e in events if e["kind"] == "call"}
    gaps = [
        e["id"]
        for e in events
        if e["config"] == model["cid"]
        and e["kind"] == "reserved"
        and e["id"].rsplit("/", 1)[0] not in recorded
    ]
    if gaps:
        raise RuntimeError("Unreceipted reservations need reconciliation: " + str(gaps))
    original_write = r.immutable_json
    old_registration = (
        r.full.RUNS / "model-restarts" / ("z-sol-argon-" + MODEL + ".json")
    )

    def write(path, value):
        if Path(path) == old_registration:
            path = old_registration.with_name("zz-argon-speed-v1.json")
        return original_write(path, value)

    r.immutable_json = write
    helper.RUNS, helper.MANIFEST, helper.PRIOR = RUNS, MANIFEST, PRIOR
    helper.prepare = lambda: f
    helper.recover_sol = lambda _: None
    helper.main()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if parser.parse_args().execute:
        main()
    else:
        print(json.dumps({"prepared": True, "workers": prepare()["workers"]}))
