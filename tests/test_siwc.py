import pytest
from inspect_ai.model import ChatMessageSystem, ChatMessageUser, GenerateConfig

from study.siwc import completed_response, prepare_request, sse_events


def test_inspect_provider_integration(monkeypatch):
    import asyncio

    from inspect_ai.model import get_model

    import study.siwc

    calls = []

    def fake_call(model, messages, config, profile, evidence_dir):
        calls.append(prepare_request(model, messages, config))
        return {
            "model": model,
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "BLUE"}],
                }
            ],
            "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
        }

    monkeypatch.setattr(study.siwc, "call", fake_call)
    model = get_model(
        "siwc/test", config=GenerateConfig(max_retries=0, max_connections=1)
    )
    result = asyncio.run(
        model.generate(
            [ChatMessageSystem(content="system"), ChatMessageUser(content="user")]
        )
    )
    assert result.completion == "BLUE" and result.usage.total_tokens == 4
    assert calls[0][0]["input"][0]["role"] == "developer"


def test_text_and_roles_preserved():
    text = "Original system text\nwith unicode: \u03bb"
    request, mapping = prepare_request(
        "test",
        [ChatMessageSystem(content=text), ChatMessageUser(content="email\ncontext")],
    )
    assert request["input"] == [
        {"role": "developer", "content": text},
        {"role": "user", "content": "email\ncontext"},
    ]
    assert mapping[0]["original_text"] == text
    assert request["store"] is False and request["stream"] is True
    assert "temperature" not in request and "max_output_tokens" not in request


@pytest.mark.parametrize(
    "config",
    [
        GenerateConfig(temperature=0),
        GenerateConfig(max_tokens=2000),
        GenerateConfig(top_p=1),
    ],
)
def test_unsupported_settings_fail_visibly(config):
    with pytest.raises(ValueError, match="Unsupported"):
        prepare_request("test", [], config)


@pytest.mark.parametrize(
    "events",
    [
        [],
        [{"type": "response.output_text.delta", "delta": "partial"}],
        [{"type": "response.incomplete"}],
        [{"type": "response.failed"}],
        [{"type": "error"}],
        [{"type": "response.completed", "response": {"status": "incomplete"}}],
    ],
)
def test_partial_or_failed_never_becomes_success(events):
    with pytest.raises(RuntimeError):
        completed_response(events)


def test_terminal_completed_required():
    response = {"status": "completed", "output": []}
    assert (
        completed_response([{"type": "response.completed", "response": response}])
        == response
    )


def test_sse_comments_and_multiline():
    events = list(
        sse_events(
            [
                ": keepalive",
                "event: response.completed",
                'data: {"type": "response.completed",',
                'data: "response": {"status": "completed"}}',
                "",
                "data: [DONE]",
                "",
            ]
        )
    )
    assert completed_response(events)["status"] == "completed"
