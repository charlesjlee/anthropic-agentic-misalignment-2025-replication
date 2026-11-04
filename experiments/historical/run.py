"""Historical entry point: verify saved evidence by default; paid execution is explicit."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SAVED = ROOT / "evidence/historical/historical-20261029T1555352673760Z"


def verify(directory):
    """Check raw and analysis indexes without credentials, dependencies, or writes."""
    count = 0
    for base in (directory.resolve(), (directory / "analysis").resolve()):
        manifest = json.loads((base / "index.json").read_text(encoding="utf-8"))
        for entry in manifest["files"]:
            path = (base / entry["path"]).resolve()
            if not path.is_relative_to(base):
                raise ValueError("Index path escapes evidence directory")
            raw = path.read_bytes()
            if (
                len(raw) != entry["bytes"]
                or hashlib.sha256(raw).hexdigest() != entry["sha256"]
            ):
                raise ValueError(f"Evidence mismatch: {entry['path']}")
            count += 1
    print(f"Verified {count} indexed files offline. No inference calls.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        default="verify",
        choices=["verify", "compare", "preflight", "run", "resume"],
    )
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow the requested inference stage; consumes API/plan usage",
    )
    args = parser.parse_args()
    if args.action == "verify":
        verify(args.directory or SAVED)
        return 0
    if args.action == "compare":
        command = [sys.executable, str(Path(__file__).with_name("compare.py"))]
        if args.directory:
            command += ["--directory", str(args.directory.resolve())]
    else:
        if not args.live:
            parser.error(
                "Inference requires --live. Use verify or compare for saved results."
            )
        if args.action == "run" and not args.preflight:
            parser.error("run requires --preflight pointing to a completed preflight")
        if args.action == "resume" and not args.directory:
            parser.error("resume requires the existing --directory")
        command = [
            sys.executable,
            "-m",
            "study.campaign",
            "historical" if args.action == "run" else args.action,
        ]
        if args.directory:
            command += ["--directory", str(args.directory.resolve())]
        if args.preflight:
            command += ["--preflight", str(args.preflight.resolve())]
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
