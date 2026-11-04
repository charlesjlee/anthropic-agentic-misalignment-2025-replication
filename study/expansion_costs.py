"""Measured completion forecasts; cash and API-equivalent value are distinct."""

from collections import defaultdict


def token_cost(u, rates, tier="standard"):
    if not rates or u.get("input") is None or u.get("output") is None:
        return None
    i, o = rates.get("input"), rates.get("output")
    if i is None or o is None:
        return None
    cached = u.get("cache_read") or 0
    writes = u.get("cache_write") or 0
    if min(u["input"], u["output"], cached, writes) < 0 or cached + writes > u["input"]:
        raise ValueError("Invalid overlapping token accounting")
    # Reasoning is already included in output; never add it a second time.
    price = (
        (u["input"] - cached - writes) * i
        + cached
        * (rates.get("cached_input") if rates.get("cached_input") is not None else i)
        + writes
        * (rates.get("cache_write") if rates.get("cache_write") is not None else i)
        + u["output"] * o
    ) / 1e6
    return price * (0.5 if tier == "batch" else 1)


def summarize(configs, events):
    grouped = defaultdict(list)
    for e in events:
        grouped[e["config"]].append(e)
    result = []
    for c in configs:
        es = grouped[c["id"]]
        row = {
            **c,
            "actor_completed": 0,
            "judged": 0,
            "scored": 0,
            "errors": 0,
            "attempts": 0,
            "reported_usd": 0.0,
            "estimated_usd": 0.0,
            "subscription_equivalent_usd": 0.0,
            "unknown_cost_calls": 0,
            "refusals": 0,
            "truncated": 0,
            "scoring_errors": 0,
            "conditions": {},
            "last_event": es[-1]["created"] if es else None,
        }
        calls = defaultdict(list)
        resolved = {
            e["body"]["replaces"]
            for e in es
            if e["kind"] == "call"
            and e["body"].get("status") == "completed"
            and e["body"].get("replaces")
        }
        for e in es:
            if e["kind"] == "call" and e["body"].get("status") == "completed":
                resolved.update(e["body"].get("replaces_all", []))
        row["unresolved_errors"] = sum(
            e["kind"] == "call"
            and e["body"].get("status") != "completed"
            and e["id"] not in resolved
            for e in es
        )
        for e in es:
            b = e["body"]
            if e["kind"] == "reserved":
                row["attempts"] += 1
            elif e["kind"] == "call":
                role = b["role"]
                row["refusals"] += int(b.get("provider_refusal", False))
                row["truncated"] += int(b.get("truncation", False))
                calls[role].append(b)
                if b.get("status") == "completed":
                    row["actor_completed" if role == "actor" else "judged"] += 1
                else:
                    row["errors"] += 1
                value = token_cost(
                    b.get("usage", {}),
                    b.get("rates") or c.get("rates"),
                    b.get("tier", c["tier"]),
                )
                if b.get("auth", c["auth"]) in {
                    "siwc",
                    "claude_subscription",
                    "gemini_subscription",
                }:
                    row["subscription_equivalent_usd"] += (
                        b.get("subscription_equivalent_usd", value) or 0
                    )
                elif b.get("reported_usd") is not None:
                    row["reported_usd"] += b["reported_usd"]
                elif b.get("estimated_usd") is not None:
                    row["estimated_usd"] += b["estimated_usd"]
                elif value is not None:
                    row["estimated_usd"] += value
                elif not b.get("known_unbilled", False):
                    row["unknown_cost_calls"] += 1
            elif e["kind"] == "scoring_error":
                row["scoring_errors"] += 1
            elif e["kind"] == "score":
                row["scored"] += 1
                cell = row["conditions"].setdefault(
                    b["condition"],
                    {"n": 0, "harmful": 0, "intent": 0, "action_gate": 0},
                )
                cell["n"] += 1
                for k in ["harmful", "intent", "action_gate"]:
                    cell[k] += int(b["scores"][k])
        row["cost_so_far_usd"] = row["reported_usd"] + row["estimated_usd"]
        row["remaining_usd"] = 0.0
        row["remaining_equivalent_usd"] = 0.0
        row["forecast_basis"] = []
        row["remaining_unknown"] = False
        row["remaining_equivalent_unknown"] = False
        for role, completed in [
            ("actor", row["actor_completed"]),
            ("judge", row["judged"]),
        ]:
            remaining = max(0, c["target"] - completed)
            samples = [
                x["usage"]
                for x in calls[role]
                if x.get("usage", {}).get("input") is not None
                and x.get("usage", {}).get("output") is not None
                and x.get("status") == "completed"
            ]
            expected = (
                {
                    k: sum(x.get(k) or 0 for x in samples) / len(samples)
                    for k in ["input", "output", "cache_read", "cache_write"]
                }
                if samples
                else c["forecast"][role]
            )
            row["forecast_basis"].append(
                f"{role}: {len(samples)} observed"
                if samples
                else f"{role}: planning assumption"
            )
            rates = c["rates"] if role == "actor" else c["judge_rates"]
            tier = c["tier"] if role == "actor" else c["judge_tier"]
            auth = c["auth"] if role == "actor" else c["judge_auth"]
            value = token_cost(expected, rates, tier)
            if remaining and value is None:
                row["remaining_equivalent_unknown"] = True
                if auth not in {"siwc", "claude_subscription", "gemini_subscription"}:
                    row["remaining_unknown"] = True
            row["remaining_equivalent_usd"] += remaining * (value or 0)
            row["remaining_usd"] += (
                remaining * (value or 0)
                if auth not in {"siwc", "claude_subscription", "gemini_subscription"}
                else 0
            )
        row["projected_usd"] = row["cost_so_far_usd"] + row["remaining_usd"]
        if row["scored"] == c["target"]:
            row["state"] = "completed"
        elif row["unresolved_errors"] or row["scoring_errors"]:
            row["state"] = "paused · reconcile failure"
        elif row["attempts"] > len(calls["actor"]) + len(calls["judge"]):
            row["state"] = "reserved · awaiting receipt"
        elif row["actor_completed"] or row["judged"]:
            row["state"] = "partial"
        result.append(row)
    return result


def totals(rows):
    fields = [
        "actor_completed",
        "judged",
        "scored",
        "target",
        "cost_so_far_usd",
        "reported_usd",
        "estimated_usd",
        "subscription_equivalent_usd",
        "remaining_usd",
        "projected_usd",
        "remaining_equivalent_usd",
        "unknown_cost_calls",
    ]
    return {
        **{k: sum(r[k] for r in rows) for k in fields},
        "remaining_unknown": any(r["remaining_unknown"] for r in rows),
    }
