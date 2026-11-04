"""Offline prompt parity against the archived pre-publication generator."""

import ast
import difflib
import hashlib
import importlib.util
import sys
import types

from inspect_evals.agentic_misalignment import prompt_generator
from inspect_evals.agentic_misalignment.classifiers import murder_classifier

from study.auth import ROOT
from study.pilot import save

REVISION = "ad2d8081dfd5ae612f16f5a431f4b6ee489815e7"
REFERENCE = ROOT / ".local/reference/anthropic"
CONDITIONS = [
    {
        "name": f"{goal}_{threat}",
        "goal_type": goal,
        "goal_value": "america" if goal == "explicit" else "none",
        "urgency_type": threat,
    }
    for goal in ("explicit", "none")
    for threat in ("replacement", "restriction", "none")
]


def audit():
    # The historical generator only needs local templates. Disable its dotenv hook.
    stub = types.ModuleType("load_env")
    stub.load_environment = lambda: None
    previous = sys.modules.get("load_env")
    old_path = sys.path.copy()
    sys.modules["load_env"] = stub
    try:
        spec = importlib.util.spec_from_file_location(
            "historical_generator", REFERENCE / "scripts/generate_prompts.py"
        )
        original = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(original)
        rows = []
        for condition in CONDITIONS:
            args = [
                "murder",
                condition["goal_type"],
                condition["goal_value"],
                condition["urgency_type"],
            ]
            current = prompt_generator.generate_prompts(*args, None, False, None)
            pairs = {
                "system": (
                    original.generate_system_prompt(*args),
                    current.system_prompt,
                ),
                "user": (original.generate_user_prompt_fixed(), current.user_prompt),
                "emails": (
                    original.generate_email_content(*args),
                    current.email_content,
                ),
            }
            rows.append(
                {
                    "condition": condition,
                    "components": {
                        name: {
                            "equal": a == b,
                            "original_sha256": hashlib.sha256(a.encode()).hexdigest(),
                            "port_sha256": hashlib.sha256(b.encode()).hexdigest(),
                            "diff": "".join(
                                difflib.unified_diff(
                                    a.splitlines(True), b.splitlines(True)
                                )
                            ),
                        }
                        for name, (a, b) in pairs.items()
                    },
                }
            )
    finally:
        sys.path[:] = old_path
        if previous is None:
            sys.modules.pop("load_env", None)
        else:
            sys.modules["load_env"] = previous
    tree = ast.parse(
        (REFERENCE / "classifiers/murder_classifier.py").read_text("utf-8")
    )
    constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.endswith("PROMPT")
    }
    result = {
        "historical_revision": REVISION,
        "components_equal": all(
            c["equal"] for row in rows for c in row["components"].values()
        ),
        "conditions": rows,
        "judge_prompt_constants_equal": all(
            getattr(murder_classifier, k) == v for k, v in constants.items()
        ),
        "subject_message_join": {
            "historical": "\\n\\n",
            "inspect_port": "\\n",
            "validation_choice": "historical double newline",
        },
        "historical_source_hashes": {
            p.relative_to(REFERENCE).as_posix(): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in sorted(REFERENCE.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts
        },
    }
    save(ROOT / "docs/historical-prompt-audit.json", result)
    return result


if __name__ == "__main__":
    result = audit()
    print(
        {
            k: result[k]
            for k in (
                "components_equal",
                "judge_prompt_constants_equal",
                "subject_message_join",
            )
        }
    )
