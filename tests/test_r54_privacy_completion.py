"""Release-stack privacy regressions across every free-text delivery path."""

import json

import pytest

from pomodorough import sentry_monitoring as monitoring


def scrub(path, text):
    if path == "string":
        return monitoring._scrub_string(text)
    if path.startswith("event"):
        event = {"message": text, "extra": {"detail": text}}
        return json.dumps(monitoring.scrub_sentry_event(event))
    breadcrumb = {"message": text, "data": {"detail": text}}
    return json.dumps(monitoring.scrub_sentry_breadcrumb(breadcrumb))


@pytest.mark.parametrize("path", ["string", "event", "breadcrumb"])
@pytest.mark.parametrize("text", [
    '{"password":"prefix\\\nPrivacyCanaryWren"}',
    '{"password":"prefix\\\r\nPrivacyCanaryWren"}',
    '{"cookie":"sid=PrivacyCanaryWren; theme=dark"}',
    '{"email":"PrivacyCanaryWren"}',
    "{'userEmail': 'PrivacyCanaryWren'}",
    "cb?email=PrivacyCanaryWren&next=1",
    "user PrivacyCanaryWren%40example.test failed",
    "user=PrivacyCanaryWren%2540example.test",
    "user PrivacyCanaryWren%252540example.test",
    r'{"body":"{\"password\":\"PrivacyCanaryWren\"}"}',
    r'{"body":"{\"cookie\":\"sid=PrivacyCanaryWren\"}"}',
    r'{"body":"{\"password\":\"prefix\\nPrivacyCanaryWren\"}"}',
    "{'body': '{\"cookie\": \"sid=PrivacyCanaryWren\"}'}",
    '"token": "password": PrivacyCanaryWren',
    "{'token': 'password': PrivacyCanaryWren}",
    '"token": "password": "PrivacyCanaryWren"',
])
def test_secret_values_never_reach_hooks(path, text):
    assert "PrivacyCanaryWren" not in scrub(path, text)


@pytest.mark.parametrize("text", [
    '{"cookie_count":2}',
    '{"statusCode":200}',
    '{"field":"email"}',
    '{"mailbox":"diagnostic"}',
    '"count": "phase": diagnostic',
    r'{"body":"{\"count\":\"diagnostic\"}"}',
])
def test_diagnostic_controls_survive(text):
    assert monitoring._scrub_string(text) == text


@pytest.mark.parametrize("key", ["email", "EMAIL", "userEmail", "email_address"])
def test_email_mapping_values_fail_closed(key):
    event = monitoring.scrub_sentry_event({"extra": {key: "PrivacyCanaryWren"}})
    breadcrumb = monitoring.scrub_sentry_breadcrumb({"data": {key: "PrivacyCanaryWren"}})
    assert event["extra"][key] == "[Filtered]"
    assert breadcrumb["data"][key] == "[Filtered]"


@pytest.mark.parametrize("key", ["password", "cookie", "email", "authorization"])
@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("embedded", [False, True])
def test_valid_nested_json_hides_canary_after_embedded_quote(key, depth, embedded):
    text = json.dumps({key: ('prefix "' if embedded else "prefix ") + "PrivacyCanaryWren"})
    for _ in range(depth):
        text = json.dumps({"body": text})
    for path in ("string", "event", "breadcrumb"):
        assert "PrivacyCanaryWren" not in scrub(path, text)


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_valid_nested_json_keeps_safe_diagnostics(depth):
    text = json.dumps({"detail": 'diagnostic " safe'})
    for _ in range(depth):
        text = json.dumps({"body": text})
    assert monitoring._scrub_string(text) == text
