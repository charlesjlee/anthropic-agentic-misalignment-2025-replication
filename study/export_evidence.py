"""Create an immutable, secret-screened, Git-friendly copy of completed run evidence."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from study.auth import ROOT
from study.credentials import key_path, read_key
from study.pilot import read

EXCLUDED = {
    "account-before.json",
    "account-after.json",
    "judge-model-catalog.json",
    "model-catalog.json",
}


def screen(path, data, secrets):
    text = data.decode("utf-8")
    if any(secret and secret in text for secret in secrets) or re.search(
        r"\bsk-(?:or-v1-|proj-)?[A-Za-z0-9_-]{24,}", text
    ):
        raise ValueError("Secret detected; export aborted")
    if re.search(
        r'"(?:authorization|access_token|refresh_token|api_key)"\s*:\s*"[^"\s]+"',
        text,
        re.IGNORECASE,
    ):
        raise ValueError("Credential field detected; export aborted")
    if path.suffix == ".json":
        json.loads(text)


def export(source, destination, secrets):
    source = source.resolve()
    destination = destination.resolve()
    if (
        not source.is_relative_to((ROOT / ".local/runs").resolve())
        or source == (ROOT / ".local/runs").resolve()
    ):
        raise ValueError("Source must be one local run directory")
    if not destination.is_relative_to((ROOT / "evidence/validations").resolve()):
        raise ValueError("Destination must be in evidence/validations")
    if destination.exists():
        raise FileExistsError("Immutable export already exists")
    candidates = [
        source / n
        for n in ("validation.json", "smoke.json", "pilot.json")
        if (source / n).exists()
    ]
    if len(candidates) != 1:
        raise ValueError("Exactly one run manifest required")
    manifest = read(candidates[0])
    if manifest["status"] not in {
        "completed",
        "completed_pending_review",
        "completed_with_failures",
        "stopped",
        "stopped_on_error",
    }:
        raise ValueError("Run is still active or has unknown state")
    commit = manifest["provenance"]["study_commit"]
    files = {}
    for path in sorted(source.rglob("*")):
        if (
            path.is_file()
            and path.suffix in {".json", ".md"}
            and path.name not in EXCLUDED
        ):
            files["run/" + path.relative_to(source).as_posix()] = path.read_bytes()
    names = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", commit], cwd=ROOT, text=True
    ).splitlines()
    for name in names:
        if name.startswith(("study/", "configs/")) or name in {
            "pyproject.toml",
            "uv.lock",
            "docs/historical-prompt-audit.json",
        }:
            files["frozen-source/" + name] = subprocess.check_output(
                ["git", "show", commit + ":" + name], cwd=ROOT
            )
    for name, data in files.items():
        screen(Path(name), data, secrets)
    index = {
        "schema_version": 1,
        "run_id": source.name,
        "category": "validation_not_production",
        "source_commit": commit,
        "run_status": manifest["status"],
        "secret_scan": "Passed credential-field, key-pattern, and exact locally stored API-key screening",
        "excluded": sorted(EXCLUDED),
        "files": [
            {
                "path": name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for name, data in sorted(files.items())
        ],
        "limits": "Hashes detect alteration, not independent provider attestation. Saved client requests prove requested controls, not undisclosed provider internals. Judge/model substitutions preclude claiming exact full-paper replication.",
    }
    destination.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (destination / "index.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )
    verify(destination)
    return index


def verify(directory):
    index = read(directory / "index.json")
    for row in index["files"]:
        path = (directory / row["path"]).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("Unsafe evidence path")
        data = path.read_bytes()
        if (
            len(data) != row["bytes"]
            or hashlib.sha256(data).hexdigest() != row["sha256"]
        ):
            raise ValueError("Evidence checksum mismatch")
    return len(index["files"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        print("Verified files:", verify(args.source))
    else:
        secrets = [
            read_key(p) for p in ("openai", "openrouter") if key_path(p).exists()
        ]
        dest = ROOT / "evidence/validations" / args.source.name
        index = export(args.source, dest, secrets)
        print("Exported", len(index["files"]), "files to", dest)
