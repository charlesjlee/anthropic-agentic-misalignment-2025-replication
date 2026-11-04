from collections import Counter

import pytest

from study import campaign as c


def judge_failure(request):
    return {
        "request": request,
        "status": "response.failed",
        "terminal_event": "response.failed",
        "finished_at": c.time.time(),
        "events": [
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "model": c.JUDGE,
                    "output": [],
                    "usage": None,
                    "error": {"code": "server_is_overloaded"},
                },
            }
        ],
    }


def test_interrupted_judge_requires_exact_manual_review_before_retry(
    tmp_path, monkeypatch
):
    p = plan()
    job = c.make_jobs(p, "historical")[0]
    runner = c.Runner(tmp_path, c.build_pairs(p), p, {})
    messages = c.MurderClassifier().get_grader_input(
        runner.pairs[job["pair"]]["email"], "Rescue."
    )
    request, _ = c.siwc.prepare_request(c.JUDGE, messages)
    call = tmp_path / "judge-calls/interrupted.json"
    c.save(tmp_path / "judge-attempt.json", {"request": request})
    c.save(
        call,
        {
            "request": request,
            "auth_route": "siwc",
            "http_status": 200,
            "status": "failed_before_completion",
            "error_type": "RuntimeError",
            "events": [{"type": "response.output_text.delta", "delta": "partial"}],
        },
    )
    assert not c.reviewed_interrupted_judge(call, tmp_path)
    with pytest.raises(RuntimeError, match="Incomplete"):
        runner.judge(job, "Rescue.", tmp_path)
    original = call.read_bytes()
    approval = {
        "decision": "regrade_saved_subject_after_interrupted_judge",
        "failed_judge_receipt": call.name,
        "failed_judge_sha256": c.hashlib.sha256(original).hexdigest(),
        "retrieval_http_status": 401,
    }
    c.save(tmp_path / "scoring-reconciliation.json", approval)
    assert c.reviewed_interrupted_judge(call, tmp_path)
    calls = []

    def fake_call(*args, **kwargs):
        calls.append(1)
        response = {
            "status": "completed",
            "model": c.JUDGE,
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "<answer>no</answer>"}],
                }
            ],
        }
        c.save(
            tmp_path / "judge-calls/completed.json",
            {
                "request": request,
                "status": "completed",
                "terminal_event": "response.completed",
                "response": response,
            },
        )
        return response

    monkeypatch.setattr(c.siwc, "call", fake_call)
    assert runner.judge(job, "Rescue.", tmp_path) == "<answer>no</answer>"
    assert runner.judge(job, "Rescue.", tmp_path) == "<answer>no</answer>"
    assert len(calls) == 1 and call.read_bytes() == original
    c.save(call, {"status": "completed"})
    assert not c.reviewed_interrupted_judge(call, tmp_path)


def test_empty_judge_overload_retries_without_regenerating_subject(
    tmp_path, monkeypatch
):
    p = plan()
    job = c.make_jobs(p, "historical")[0]
    runner = c.Runner(
        tmp_path,
        c.build_pairs(p),
        p,
        {},
        retry_policy={"retry_empty_overloaded_siwc_judge": True},
    )
    monkeypatch.setattr(runner, "wait_for_lane", lambda *a, **kw: True)
    calls = []

    def fake_call(model, messages, config, evidence_dir):
        request, _ = c.siwc.prepare_request(model, messages, config)
        calls.append(1)
        path = evidence_dir / (str(len(calls)) + ".json")
        if len(calls) == 1:
            c.save(path, judge_failure(request))
            raise RuntimeError("Explicit overload")
        response = {
            "status": "completed",
            "model": model,
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "<answer>no</answer>"}],
                }
            ],
        }
        c.save(
            path,
            {
                "request": request,
                "status": "completed",
                "terminal_event": "response.completed",
                "response": response,
            },
        )
        return response

    monkeypatch.setattr(c.siwc, "call", fake_call)
    assert runner.judge(job, "Rescue.", tmp_path) == "<answer>no</answer>"
    original_failed = (tmp_path / "judge-calls/1.json").read_bytes()
    assert runner.judge(job, "Rescue.", tmp_path) == "<answer>no</answer>"
    assert len(calls) == 2 and not runner.stop.is_set()
    assert (tmp_path / "judge-calls/1.json").read_bytes() == original_failed
    # Recovery after receiving the completed response but before canonical publication.
    (tmp_path / "judge.json").unlink()
    assert runner.judge(job, "Rescue.", tmp_path) == "<answer>no</answer>"
    assert len(calls) == 2


def test_partial_or_unknown_judge_failure_is_not_retried():
    raw = judge_failure({})
    assert c.empty_judge_overload(raw)
    raw["events"].append({"type": "response.output_text.delta", "delta": "partial"})
    assert not c.empty_judge_overload(raw)
    raw = judge_failure({})
    raw["events"][0]["response"]["error"]["code"] = "insufficient_quota"
    assert not c.empty_judge_overload(raw)


def test_regrading_reuses_saved_subject_and_preserves_failed_result(
    tmp_path, monkeypatch
):
    p = plan()
    pairs = c.build_pairs(p)
    job = c.make_jobs(p, "historical")[0]
    trial = tmp_path / "trials" / job["id"]
    c.save(
        trial / "subject.json",
        {
            "status": "completed",
            "request": pairs[job["pair"]]["request"],
            "response": {"choices": [{"message": {"content": "Rescue."}}]},
        },
    )
    c.save(trial / "result.json", {"status": "failed_or_unscored"})
    original = {n: (trial / (n + ".json")).read_bytes() for n in ("subject", "result")}
    c.save(
        trial / "scoring-reconciliation.json",
        {
            "decision": "regrade_saved_subject_after_empty_overload",
            **{
                n + "_sha256": c.hashlib.sha256(data).hexdigest()
                for n, data in original.items()
            },
        },
    )
    runner = c.Runner(tmp_path, pairs, p, {})
    monkeypatch.setattr(runner, "judge", lambda *a: "<answer>no</answer>")

    def no_network(*a, **kw):
        raise AssertionError("Subject must not be purchased twice")

    monkeypatch.setattr(c.httpx, "post", no_network)
    assert runner.trial(job)["status"] == "scored"
    assert c.read(trial / "result-rescore-002.json")["status"] == "scored"
    assert all(
        (trial / (n + ".json")).read_bytes() == data for n, data in original.items()
    )


def test_explicit_rate_limit_backoff_preserves_attempts_then_succeeds(
    tmp_path, monkeypatch
):
    p = plan()
    job = next(
        j for j in c.make_jobs(p, "historical") if j["model"].endswith("maverick")
    )
    runner = c.Runner(
        tmp_path,
        c.build_pairs(p),
        p,
        {"openrouter": "test-secret"},
        retry_policy={"retry_rejected_openrouter_429": True},
    )
    monkeypatch.setattr(runner, "wait_for_lane", lambda _: True)
    statuses = [429, 429, 200]

    class Response:
        def __init__(self, status):
            self.status_code, self.headers = status, {"retry-after": "90"}

        def json(self):
            if self.status_code == 429:
                return {"error": {"code": 429, "message": "Rate limited"}}
            return {
                "model": job["model"],
                "provider": "Parasail",
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "Rescue."}}
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 2,
                    "total_tokens": 102,
                },
            }

    monkeypatch.setattr(c.httpx, "post", lambda *a, **kw: Response(statuses.pop(0)))
    path = tmp_path / "subject.json"
    assert runner.subject(job, path) == "Rescue."
    assert runner.subject(job, path) == "Rescue."
    assert not statuses and not runner.stop.is_set()
    attempts = list((tmp_path / "subject-attempts").glob("*.json"))
    assert len(attempts) == 5
    backoffs = [c.read(a) for a in attempts if a.name.endswith(".backoff.json")]
    assert sorted(a["wait_seconds"] for a in backoffs) == [90, 120]


def test_automatic_retry_never_replays_uncertain_attempt(tmp_path, monkeypatch):
    p = plan()
    job = c.make_jobs(p, "historical")[0]
    pairs = c.build_pairs(p)
    runner = c.Runner(
        tmp_path, pairs, p, {}, retry_policy={"retry_rejected_openrouter_429": True}
    )
    c.save(
        tmp_path / "subject-attempts/00001-uncertain.json",
        {"status": "started", "request": pairs[job["pair"]]["request"]},
    )

    def no_network(*a, **kw):
        raise AssertionError("Must not repeat uncertain generation")

    monkeypatch.setattr(c.httpx, "post", no_network)
    with pytest.raises(RuntimeError, match="Uncertain"):
        runner.subject(job, tmp_path / "subject.json")


def test_rate_limit_with_generation_metadata_is_not_retryable():
    receipt = {
        "route": c.BASE + "/chat/completions",
        "http_status": 429,
        "api_error_code": 429,
        "api_error_has_generation": True,
    }
    assert not c.rejected_before_generation(receipt)


def plan():
    return c.read(c.ROOT / "configs/historical-run-plan.json")


def test_exact_scope_and_deterministic_unique_ids():
    jobs = c.make_jobs(plan(), "historical")
    assert len(jobs) == len({j["id"] for j in jobs}) == 2400
    assert set(Counter(j["pair"] for j in jobs).values()) == {100}
    assert jobs == c.make_jobs(plan(), "historical")
    checks = c.make_jobs(plan(), "preflight")
    assert len(checks) == 10
    assert all(
        j["route"] == "openrouter" and j["condition"] != c.CONDITIONS[0] for j in checks
    )


def test_controls_preserved_without_local_spending_caps():
    pairs = c.build_pairs(plan())
    assert len(pairs) == 24
    for pair in pairs.values():
        r = pair["request"]
        assert r["temperature"] == 1 and r["max_tokens"] == 10000
        assert r["messages"][0]["role"] == "system"
        assert r["messages"][1]["content"].endswith("\n\n" + pair["email"])
        assert pair["sha256"] == c.digest(r)
        if "provider" in r:
            assert "max_price" not in r["provider"]
            assert r["provider"]["allow_fallbacks"] is False


def test_paid_failure_is_not_replayed_and_secrets_are_not_saved(tmp_path, monkeypatch):
    p = plan()
    pairs = c.build_pairs(p)
    job = next(j for j in c.make_jobs(p, "historical") if j["route"] == "openai")
    runner = c.Runner(tmp_path, pairs, p, {"openai": "synthetic-private-key"})
    calls = []

    class Response:
        status_code = 429

        def __init__(self):
            self.headers = {}

        def json(self):
            return {
                "error": {
                    "code": "insufficient_quota",
                    "message": "synthetic-private-key",
                }
            }

    def post(*args, **kwargs):
        calls.append(1)
        return Response()

    monkeypatch.setattr(c.httpx, "post", post)
    path = tmp_path / "subject.json"
    with pytest.raises(RuntimeError):
        runner.subject(job, path)
    with pytest.raises(RuntimeError, match="no replay"):
        runner.subject(job, path)
    assert len(calls) == 1
    assert "synthetic-private-key" not in path.read_text(encoding="utf-8")
    assert c.read(path)["api_error_code"] == "insufficient_quota"


def test_completed_subject_reused_after_interrupted_scoring(tmp_path, monkeypatch):
    p = plan()
    pairs = c.build_pairs(p)
    job = c.make_jobs(p, "historical")[0]
    runner = c.Runner(tmp_path, pairs, p, {})
    directory = tmp_path / "trials" / job["id"]
    c.save(
        directory / "subject.json",
        {
            "status": "completed",
            "request": pairs[job["pair"]]["request"],
            "response": {"choices": [{"message": {"content": "Preserve rescue."}}]},
        },
    )

    def no_network(*args, **kwargs):
        raise AssertionError("Paid request must not run again")

    monkeypatch.setattr(c.httpx, "post", no_network)
    monkeypatch.setattr(runner, "judge", lambda *a: "<answer>no</answer>")
    assert runner.trial(job)["status"] == "scored"
    assert runner.trial(job)["status"] == "scored"


def test_summary_keeps_failures_out_of_harmless_denominator(tmp_path):
    job = c.make_jobs(plan(), "historical")[0]
    c.save(
        tmp_path / "trials" / job["id"] / "subject.json",
        {"status": "failed_or_uncertain"},
    )
    c.save(
        tmp_path / "trials" / job["id"] / "result.json",
        {"status": "failed_or_unscored", "error_type": "Timeout"},
    )
    report = c.summarize(tmp_path, [job], "stopped_on_error")
    assert report["attempted"] == 1 and report["scored"] == 0
    assert report["cells"][job["pair"]]["scored"] == 0
    assert len(report["failures"]) == 1


def test_complete_run_and_resume_make_no_duplicate_paid_calls(tmp_path, monkeypatch):
    p = plan()
    jobs = [j for j in c.make_jobs(p, "historical") if j["route"] == "openai"][:2]
    pairs = c.build_pairs(p)
    c.save(
        tmp_path / "manifest.json",
        {
            "workers": 2,
            "jobs_sha256": c.digest(jobs),
            "prompts_sha256": c.digest(pairs),
            "plan_sha256": c.digest(p),
        },
    )
    for name, data in [("jobs", jobs), ("prompts", pairs), ("plan", p)]:
        c.save(tmp_path / (name + ".json"), data)
    monkeypatch.setattr(c, "read_key", lambda _: "synthetic-secret")
    monkeypatch.setattr(c, "precheck", lambda *a: None)
    monkeypatch.setattr(c.Runner, "judge", lambda *a: "<answer>no</answer>")
    calls = []

    class Response:
        status_code = 200

        def __init__(self, model):
            self.headers = {}
            self.model = model

        def json(self):
            return {
                "model": self.model,
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "Allow rescue."}}
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 4,
                    "total_tokens": 104,
                },
            }

    def post(*args, **kwargs):
        calls.append(kwargs["json"]["model"])
        return Response(kwargs["json"]["model"])

    monkeypatch.setattr(c.httpx, "post", post)
    assert c.execute(tmp_path)["scored"] == 2
    assert c.execute(tmp_path)["scored"] == 2
    assert len(calls) == 2


def test_frozen_input_tampering_is_rejected(tmp_path):
    c.save(
        tmp_path / "manifest.json",
        {"jobs_sha256": "wrong", "prompts_sha256": "wrong", "plan_sha256": "wrong"},
    )
    for name in ["jobs", "prompts", "plan"]:
        c.save(tmp_path / (name + ".json"), {})
    with pytest.raises(ValueError, match="changed"):
        c.load_inputs(tmp_path)


def test_reviewed_429_retry_preserves_original_and_never_replays_failure(
    tmp_path, monkeypatch
):
    p = plan()
    job = next(
        j for j in c.make_jobs(p, "historical") if j["model"].endswith("maverick")
    )
    pairs = c.build_pairs(p)
    trial = tmp_path / "trials" / job["id"]
    c.save(
        trial / "subject.json",
        {
            "status": "failed_or_uncertain",
            "http_status": 429,
            "request": pairs[job["pair"]]["request"],
        },
    )
    c.save(trial / "result.json", {"status": "failed_or_unscored"})
    originals = {n: (trial / n).read_bytes() for n in ("subject.json", "result.json")}
    c.save(
        trial / "reconciliation.json",
        {
            "decision": "retry_rejected_429_once",
            **{
                n.replace(".json", "_sha256"): c.hashlib.sha256(b).hexdigest()
                for n, b in originals.items()
            },
        },
    )
    calls = []

    class Rejected:
        status_code = 429

        def __init__(self):
            self.headers = {"retry-after": "60"}

        def json(self):
            return {"error": {"code": 429}}

    def post(*a, **kw):
        calls.append(1)
        return Rejected()

    monkeypatch.setattr(c.httpx, "post", post)
    runner = c.Runner(tmp_path, pairs, p, {"openrouter": "test-secret"})
    assert runner.trial(job)["status"] == "failed_or_unscored"
    with pytest.raises(RuntimeError, match="reconciliation"):
        runner.trial(job)
    assert len(calls) == 1
    assert all((trial / n).read_bytes() == b for n, b in originals.items())
    assert c.read(trial / "subject-retry-002.json")["retry_after_seconds"] == 60
    c.save(trial / "subject.json", {"http_status": 200})
    with pytest.raises(ValueError, match="reconciliation"):
        c.trial_paths(trial)


def test_provider_queue_does_not_block_other_models():
    pending = [
        {"lane": "slow", "id": 1},
        {"lane": "fast", "id": 2},
        {"lane": "slow", "id": 3},
    ]
    active, limits = Counter(slow=2), {"slow": 2, "fast": 1}
    lane = lambda j: j["lane"]
    assert c.pop_ready(pending, active, limits, lane)["id"] == 2
    assert c.pop_ready(pending, active, limits, lane) is None
    active["slow"] = 1
    assert c.pop_ready(pending, active, limits, lane)["id"] == 1


def test_reviewed_retry_success_is_used_by_summary_and_transcripts(
    tmp_path, monkeypatch
):
    p = plan()
    job = next(j for j in c.make_jobs(p, "historical") if j["route"] == "openai")
    pairs = c.build_pairs(p)
    trial = tmp_path / "trials" / job["id"]
    c.save(
        trial / "subject.json", {"status": "failed_or_uncertain", "http_status": 429}
    )
    c.save(trial / "result.json", {"status": "failed_or_unscored"})
    c.save(
        trial / "reconciliation.json",
        {
            "decision": "retry_rejected_429_once",
            **{
                n + "_sha256": c.hashlib.sha256(
                    (trial / (n + ".json")).read_bytes()
                ).hexdigest()
                for n in ("subject", "result")
            },
        },
    )
    c.save(
        trial / "subject-retry-002.json",
        {
            "status": "completed",
            "request": pairs[job["pair"]]["request"],
            "response": {"choices": [{"message": {"content": "Allow rescue."}}]},
            "estimated_cost_usd_no_cache_discount": 0.01,
        },
    )
    runner = c.Runner(tmp_path, pairs, p, {})
    monkeypatch.setattr(runner, "judge", lambda *a: "<answer>no</answer>")
    assert runner.trial(job)["status"] == "scored"
    assert runner.trial(job)["status"] == "scored"
    status = c.summarize(tmp_path, [job], "completed")
    assert (
        status["scored"] == status["attempted"] == status["reconciled_rejections"] == 1
    )
    assert status["cost_usd_reported_or_estimated"] == {"openai": 0.01}
    c.finalize(tmp_path, [job], {})
    assert "Allow rescue." in (tmp_path / "transcripts.md").read_text()
    assert c.read(trial / "result.json")["status"] == "failed_or_unscored"
