"""One reconciled retry or user-authorized incomplete replacement; never replace completed output."""

from study.expansion_setup import artifact, read


def attempt_location(directory, event_id):
    authorization = directory / "retry-authorization.json"
    if not authorization.exists():
        return directory, event_id, None
    permission = read(authorization)
    receipt_path = directory / "receipt.json"
    prior = read(receipt_path)
    if permission["prior_receipt"] != artifact(receipt_path):
        raise ValueError("Retry authorization receipt hash mismatch")
    rejected = (
        prior.get("status") == "http_error"
        and prior.get("http_status") in {429, 503}
        and not prior.get("response")
        and not prior.get("terminal_event")
        and not (directory / "stream.jsonl").exists()
    )
    replacement = (
        permission.get("disposition") == "user_authorized_incomplete_replacement"
        and prior.get("status") == "uncertain"
        and prior.get("http_status") == 200
        and prior.get("error_type") == "RemoteProtocolError"
        and not prior.get("terminal_event")
        and not prior.get("response")
    )
    if replacement:
        import json

        events = [
            json.loads(line)
            for line in (directory / "stream.jsonl").read_text("utf-8").splitlines()
        ]
        replacement = not any(e.get("type") == "response.completed" for e in events)
    if not (rejected or replacement):
        raise ValueError(
            "Retry requires a rejected request or explicit incomplete-replacement authorization"
        )
    return (
        directory / "retry-001",
        event_id + "/retry-001",
        {
            "replaces": event_id,
            "authorization": artifact(authorization),
            "recovery_disposition": permission.get(
                "disposition", "rejected_request_retry"
            ),
        },
    )
