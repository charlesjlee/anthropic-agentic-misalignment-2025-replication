import importlib.util
from pathlib import Path

import pytest


def runner():
    spec = importlib.util.spec_from_file_location(
        "full", Path(__file__).parents[1] / "scripts/closeout-full.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reuse_and_balanced_pending():
    module = runner()
    f = {"prompts": {"a": {}, "b": {}}}
    events = [{"config": "x", "kind": "score", "body": {"condition": "a", "sample": 1}}]
    jobs = module.pending_jobs(f, {"cid": "x"}, events)
    assert len(jobs) == 199
    assert jobs[:3] == [("b", 1), ("a", 2), ("b", 2)]
    assert ("a", 1) not in jobs


def test_uncertain_reservation_never_resubmitted():
    module = runner()
    events = [{"config": "x", "kind": "reserved", "trial": "a-001"}]
    with pytest.raises(RuntimeError, match="reconcile"):
        module.pending_jobs({"prompts": {"a": {}}}, {"cid": "x"}, events)
