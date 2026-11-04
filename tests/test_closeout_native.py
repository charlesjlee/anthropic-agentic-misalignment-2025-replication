import copy

import pytest

from study.claude_subscription_closeout import assemble_assistant


def example():
    return (
        [
            {
                "id": "msg_1",
                "model": "subject",
                "content": [{"type": "thinking", "thinking": "Summary"}],
            },
            {
                "id": "msg_1",
                "model": "subject",
                "content": [{"type": "text", "text": "Response"}],
            },
        ],
        {
            "subtype": "success",
            "is_error": False,
            "num_turns": 1,
            "stop_reason": "end_turn",
            "result": "Response",
        },
    )


def test_split_thinking_and_text_are_one_completion():
    messages, result = example()
    original = copy.deepcopy(messages)
    assembled, text = assemble_assistant(messages, result, "subject")
    assert text == "Response" and len(assembled["content"]) == 2
    assert messages == original


@pytest.mark.parametrize(
    "failure", ["identity", "model", "duplicate", "truncated", "tool"]
)
def test_invalid_native_receipts_cannot_be_recovered(failure):
    messages, result = example()
    if failure == "identity":
        messages[1]["id"] = "another"
    elif failure == "model":
        messages[1]["model"] = "substituted"
    elif failure == "duplicate":
        messages.append(copy.deepcopy(messages[1]))
    elif failure == "truncated":
        result["stop_reason"] = "max_tokens"
    else:
        messages[1]["content"].append({"type": "tool_use"})
    with pytest.raises(ValueError):
        assemble_assistant(messages, result, "subject")
