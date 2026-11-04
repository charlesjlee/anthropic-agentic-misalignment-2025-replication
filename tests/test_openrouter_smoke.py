import pytest

from study import openrouter_smoke as s
from study.pilot import read


def test_two_route_budget_and_no_fallback():
    config = read(s.ROOT / "configs/openrouter-validation.json")
    request, _ = s.request_for("gpt-4.1-2025-04-14", s.CONDITIONS[0])
    costs = []
    for route in config["models"]:
        request["provider"] = route["provider"]
        costs.append(s.budget_for(request))
    assert sum(costs) == pytest.approx(0.0488)
    request["provider"]["allow_fallbacks"] = True
    with pytest.raises(ValueError, match="pinned"):
        s.budget_for(request)


def test_reject_truncation_or_provider_substitution_and_preserve_content_only():
    response = {
        "model": "test",
        "provider": "SiliconFlow",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "final", "reasoning": "separate"},
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
    }
    assert s.checked_completion(response, "test", "SiliconFlow") == "final"
    with pytest.raises(ValueError, match="provider"):
        s.checked_completion(response, "test", "Parasail")
    response["choices"][0]["finish_reason"] = "length"
    with pytest.raises(ValueError, match="Incomplete"):
        s.checked_completion(response, "test", "SiliconFlow")
