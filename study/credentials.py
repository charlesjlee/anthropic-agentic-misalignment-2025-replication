"""Project API keys: hidden terminal entry and Windows user-scoped DPAPI storage.

No network requests. Keys are returned only to Python callers, never by the CLI.
"""

import argparse
import getpass
import sys
import warnings

from filelock import FileLock

from study.auth import AUTH, load, save

PROVIDERS = ("openai", "openrouter", "huggingface")


def key_path(provider):
    if provider not in PROVIDERS:
        raise ValueError("Unsupported credential provider")
    return AUTH / f"api-{provider}.dpapi"


def validate_key(value):
    if (
        not isinstance(value, str)
        or len(value) < 20
        or not value.isascii()
        or any(c.isspace() or not c.isprintable() for c in value)
    ):
        raise ValueError("Invalid key format; key was not saved")
    return value


def read_key(provider):
    """Load for explicit api_key arguments; do not print or log the result."""
    record = load(key_path(provider))
    if record.get("provider") != provider:
        raise ValueError("Credential provider mismatch")
    return validate_key(record.get("api_key"))


def store_key(provider, value, *, replace=False):
    path = key_path(provider)
    validate_key(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock", timeout=10):
        if path.exists() and not replace:
            raise FileExistsError("Key already stored; use --replace to rotate it")
        save(path, {"provider": provider, "api_key": value})


def enter_key(provider, *, replace=False):
    if not sys.stdin.isatty():
        raise RuntimeError("Run key entry in your own interactive PowerShell window")
    # Fail closed rather than accepting getpass's echoing stdin fallback.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        value = getpass.getpass(f"Paste {provider} API key (hidden): ")
    store_key(provider, value, replace=replace)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("set", "status"))
    parser.add_argument("provider", choices=PROVIDERS)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "set":
            enter_key(args.provider, replace=args.replace)
            print("Key saved with Windows user-scoped encryption. No API call made.")
        elif not key_path(args.provider).exists():
            print("No project key stored.")
        else:
            read_key(args.provider)
            print("Project key stored and decryptable. API access has not been tested.")
    except (KeyboardInterrupt, EOFError):
        print("Key entry cancelled.", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - CLI secret boundary must suppress exception text
        # Exceptions from storage/parsing must never expose credential contents.
        print(
            "Credential operation failed. Use an interactive terminal under your "
            "Windows account; check the key format and use --replace only to rotate "
            "an existing key. No key value is printed.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
