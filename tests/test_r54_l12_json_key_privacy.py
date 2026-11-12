"""Stringified secret keys and cookie headers stay private on every Sentry path."""

import json
import time

import pytest

from pomodorough import sentry_monitoring as monitoring

_PATHS = ("string", "event_message", "event_extra", "breadcrumb_message", "breadcrumb_data")
_MISSING_KEYS = ("cookie", "authorization", "auth", "dsn", "credential", "cookies", "code", "set-cookie")


def _scrub_path(path, text):
    if path == "string":
        return monitoring._scrub_string(text)
    if path == "event_message":
        return monitoring.scrub_sentry_event({"message": text})["message"]
    if path == "event_extra":
        return monitoring.scrub_sentry_event({"extra": {"detail": text}})["extra"]["detail"]
    if path == "breadcrumb_message":
        return monitoring.scrub_sentry_breadcrumb({"message": text})["message"]
    return monitoring.scrub_sentry_breadcrumb({"data": {"detail": text}})["data"]["detail"]


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("key", _MISSING_KEYS)
@pytest.mark.parametrize("shape", ("double", "single"))
def test_missing_json_key_value_stays_private(path, key, shape):
    canary = "QueryCanaryWren"
    quote = '"' if shape == "double" else "'"
    text = f"{{{quote}{key}{quote}:{quote}sid={canary}; theme=dark{quote}}}"
    expected = f"{{{quote}{key}{quote}:{quote}[Filtered]{quote}}}"
    cleaned = _scrub_path(path, text)
    assert canary not in cleaned
    assert cleaned == expected


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("key", _MISSING_KEYS)
@pytest.mark.parametrize("quote", ('"', "'"))
def test_truncated_missing_json_value_stays_private(path, key, quote):
    text = f"{{{quote}{key}{quote}:{quote}TruncatedCanaryWren"
    cleaned = _scrub_path(path, text)
    assert "TruncatedCanaryWren" not in cleaned
    assert cleaned == f"{{{quote}{key}{quote}:{quote}[Filtered]"


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("key", ("token", "session_id", "room_id", "privateKey"))
@pytest.mark.parametrize("quote", ('"', "'"))
def test_truncated_legacy_json_value_stays_filtered(path, key, quote):
    text = f"{{{quote}{key}{quote}:{quote}private value"
    assert _scrub_path(path, text) == f"{{{quote}{key}{quote}:{quote}[Filtered]"


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("header", ("Cookie", "Set-Cookie"))
@pytest.mark.parametrize("shape", ("plain", "lowercase", "multiline"))
def test_cookie_header_value_stays_private(path, header, shape):
    label = header.lower() if shape == "lowercase" else header
    prefix, suffix = ("GET / HTTP/1.1\r\n", "\r\nAccept: text/plain") if shape == "multiline" else ("", "")
    text = f"{prefix}{label}: sid=HeaderCanaryWren; theme=dark{suffix}"
    cleaned = _scrub_path(path, text)
    assert "HeaderCanaryWren" not in cleaned
    assert cleaned == f"{prefix}{label}: [Filtered]{suffix}"


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("text", (
    '{"cookie_count":2}', '{"errorcode":5}', '{"statuscode":5}', '{"exitcode":5}',
    '{"encode":"utf-8"}', '{"decode":"utf-8"}', '{"codec":"utf-8"}',
    "room=5", "encode x", "Cookie count: 2", "Set-Cookie count: 2",
    '{"monkey":"banana"}', '{"keyboard":"connected"}',
))
def test_static_diagnostics_stay_unchanged(path, text):
    assert _scrub_path(path, text) == text


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("key", ("myCredential", "session_cookie", "cookie_count"))
def test_mapping_sensitive_compound_strings_stay_private(path, key):
    text = f'{{"{key}":"CompoundCanaryWren"}}'
    assert _scrub_path(path, text) == f'{{"{key}":"[Filtered]"}}'


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("key", ("author's_cookie", "cookie\\suffix", 'cookie"suffix'))
@pytest.mark.parametrize("quote", ('"', "'"))
def test_quoted_mapping_sensitive_keys_stay_private(path, key, quote):
    assert monitoring._sensitive_key(key)
    encoded = key.replace("\\", "\\\\").replace(quote, "\\" + quote)
    text = f"{{{quote}{encoded}{quote}:{quote}QuotedKeyCanaryWren{quote}}}"
    assert _scrub_path(path, text) == f"{{{quote}{encoded}{quote}:{quote}[Filtered]{quote}}}"


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("key", ("codec'format", "format\\codec", 'codec"format'))
@pytest.mark.parametrize("quote", ('"', "'"))
def test_quoted_safe_keys_stay_unchanged(path, key, quote):
    assert not monitoring._sensitive_key(key)
    encoded = key.replace("\\", "\\\\").replace(quote, "\\" + quote)
    text = f"{{{quote}{encoded}{quote}:{quote}utf-8{quote}}}"
    assert _scrub_path(path, text) == text


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("key", ("cookie_count", "myCredential", "session_cookie"))
@pytest.mark.parametrize("scalar", ("2", "true", "false", "null"))
def test_new_compound_scalars_stay_unchanged(path, key, scalar):
    text = f'{{"{key}":{scalar}}}'
    assert _scrub_path(path, text) == text


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("key", ("token", "session_id", "room_id", "privateKey"))
@pytest.mark.parametrize("scalar", ("2", "true", "false", "null"))
def test_existing_canonical_scalars_stay_filtered(path, key, scalar):
    assert _scrub_path(path, f'{{"{key}":{scalar}}}') == f'{{"{key}":[Filtered]}}'


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("text", (
    '{"cookie":"head\\"EscapedCanaryWren; tail=1"}',
    '{"cookie":"head\'EscapedCanaryWren&tail=1"}',
    "{'cookie':'head\\'EscapedCanaryWren?tail=1'}",
))
def test_json_string_escapes_and_separators_stay_private(path, text):
    cleaned = _scrub_path(path, text)
    assert "EscapedCanaryWren" not in cleaned
    quote = text[1]
    assert cleaned == f"{{{quote}cookie{quote}:{quote}[Filtered]{quote}}}"


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("shape", ("repr_json_body", "json_single_pair", "pair_inside_key"))
def test_nested_pairs_preserve_live_privacy(path, shape):
    canary = "NestedCanaryWren"
    text = {
        "repr_json_body": repr({"body": json.dumps({"password": canary})}),
        "json_single_pair": json.dumps({"msg": f"retry 'token':'{canary}'"}),
        "pair_inside_key": f'''"'sid':'{canary}":"'"''',
    }[shape]
    cleaned = _scrub_path(path, text)
    assert canary not in cleaned, (text, cleaned)
    assert "[Filtered]" in cleaned


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("length", (17, 127, 128))
def test_sensitive_keys_near_capture_bound_stay_private(path, length):
    key = "x" * (length - len("cookie")) + "cookie"
    assert len(key) == length
    assert monitoring._sensitive_key(key)
    text = json.dumps({key: "LongKeyCanaryWren"})
    assert _scrub_path(path, text) == json.dumps({key: "[Filtered]"})


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("text", (
    '''{"token":"ctx {'note':'he"ad MalformedCanaryWren', 'detail':'MalformedTailCanaryWren'}","n":"public"}''',
    '''{"token":"ctx {"note":"MalformedCanaryWren", "detail":"MalformedTailCanaryWren"}","n":"public"}''',
    '''{"token":"ctx [{'note':'he"ad MalformedCanaryWren'}, {'detail':'MalformedTailCanaryWren'}]","n":"public"}''',
    '''{"token":"ctx {"authorization":ScalarCanaryWren, 'n':"ScalarTailCanaryWren"}","n":"public"}''',
    '''{"token":"ctx {'msg': ScalarCanaryWren, "codec":"ScalarTailCanaryWren"}","n":"public"}''',
    '''{"token":"ctx {'msg': ScalarCanaryWren, 'codec':'ScalarTailCanaryWren'}","n":"public"}''',
))
def test_malformed_nested_values_inherit_secret_privacy(path, text):
    assert _scrub_path(path, text) == '{"token":"[Filtered]","n":"public"}'


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("text", ('''{'password":"MismatchedCanaryWren"}''', '''{"password':'MismatchedCanaryWren'}'''))
def test_mismatched_key_quotes_preserve_live_privacy(path, text):
    assert "MismatchedCanaryWren" not in _scrub_path(path, text)


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("text,expected", (
    ('{"password":"pAndroidCanaryWren&a b=1"}', '{"password":"[Filtered]"}'),
    ('{"cookie":"sid=AndroidCanaryWren; theme=dark"}', '{"cookie":"[Filtered]"}'),
    ('{"token":"AndroidCanaryWren&x","n":"a=1"}', '{"token":"[Filtered]","n":"a=1"}'),
    ("{'password': 'pAndroidCanaryWren&a b=1'}", "{'password': '[Filtered]'}"),
    ('{"session":"AndroidCanaryWren?a b=1"}', '{"session":"[Filtered]"}'),
))
def test_android_separator_rows_keep_safe_tails(path, text, expected):
    assert _scrub_path(path, text) == expected


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("key", ("clientToken", "signingKeys", "CODE"))
def test_triage_compound_and_uppercase_keys_stay_private(path, key):
    text = json.dumps({key: "TriageCanaryWren"})
    assert "TriageCanaryWren" not in _scrub_path(path, text)


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("text", (
    '''{"token":"a"password":"TriageCanaryWren"}''',
    "{'token':'a'password':'TriageCanaryWren'}",
    '{"a":"token":"TriageCanaryWren"}',
    "{'a':'token':'TriageCanaryWren'}",
    '{"a": "token": "TriageCanaryWren"}',
    '{"a":"token":12345678}',
    '''"signingKey": "see 'token': '" TriageCanaryWren' ''',
    '''{'signingKey': 'see"cookie": "x'TriageCanaryWren"}''',
), ids=("double_back_to_back", "single_back_to_back", "double_value_then_key",
        "single_value_then_key", "spaced_value_then_key", "scalar_value_then_key",
        "mixed_compound_double_value_meets_single_pair", "mixed_compound_single_value_meets_double_pair"))
def test_triage_overlapping_pairs_stay_private(path, text):
    cleaned = _scrub_path(path, text)
    assert "TriageCanaryWren" not in cleaned
    assert "12345678" not in cleaned
    assert "[Filtered]" in cleaned


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("text", (
    '''"uploadToken": "z 'token': 'p" MixedCanaryWrenA' ''',
    '{"token@ex.io": "MixedCanaryWrenB"}',
    '''{'uploadToken': 'MixedCanaryWrenC', "authKey": "MixedCanaryWrenD"}''',
    '''{"uploadToken": "MixedCanaryWrenE 'x':'y'", 'signingKey': 'MixedCanaryWrenF'}''',
    '''"x'clientToken':'MixedCanaryWrenG":"'"''',
    ''''x"clientToken":"MixedCanaryWrenH':'"' ''',
    '''{'clientToken': 'a@b.io"k": "MixedCanaryWrenI', "n": "x@y.io"}''',
    '''{'n': 'x', "uploadToken": "Bearer MixedCanaryWrenJ"}''',
    '''"x": "a@b.io", 'secretKey': 'MixedCanaryWrenK' ''',
), ids=("upload_meets_token", "email_in_key", "two_compound_keys", "mixed_compound_values",
        "single_pair_in_double_key", "double_pair_in_single_key", "email_in_single_value",
        "bearer_value", "email_then_secret"))
def test_android_mixed_quote_witnesses_stay_private(path, text):
    assert "MixedCanaryWren" not in _scrub_path(path, text)


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("text,canaries", (
    ('{"password": "QzB01W\\\nQzB02W"}', ("QzB01W", "QzB02W")),
    ("{'token': 'QzB03W\\\r\nQzB04W'}", ("QzB03W", "QzB04W")),
    ('{"password":"pQzF01W\\\nQzF01L&a b=1"}', ("QzF01W", "QzF01L")),
    ('{"password": "QzB08W\nQzB09W"}', ("QzB08W", "QzB09W")),
    ('{"password": "QzB10W\rQzB11W"}', ("QzB10W", "QzB11W")),
    ('{"password": "QzB12W\\\rQzB13W"}', ("QzB12W", "QzB13W")),
), ids=("backslash_newline_in_secret", "backslash_crlf_in_secret", "seed7_companion",
        "bare_lf", "bare_cr", "backslash_cr"))
def test_line_breaks_inside_secret_values_stay_private(path, text, canaries):
    cleaned = _scrub_path(path, text)
    assert all(canary not in cleaned for canary in canaries), (text, cleaned)
    assert "[Filtered]" in cleaned


@pytest.mark.parametrize("path", _PATHS)
def test_backslash_at_absolute_end_preserves_truncated_value_boundary(path):
    text = '{"password": "QzB07W' + "\\"
    assert _scrub_path(path, text) == '{"password": "[Filtered]' + "\\"


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("text", (
    "\"refresh_token_for_account_x':'{{Qz0000W",
    '\n\n"refresh_token_for_account_x\' : "refresh_token_for_account_x\'{\n"token": Qz0000W',
    "\"token'\": \"token\"Qz0000W[\": Qz0001W",
    "x: 'token\"' : 'Qz0000W",
), ids=("N2_malformed_long_key", "N3_unclosed_container", "N6_overlapping_pair", "N8_spaced_colon"))
def test_acceptor_survivor_witnesses_stay_private(path, text):
    cleaned = _scrub_path(path, text)
    assert "Qz0000W" not in cleaned, (text, cleaned)
    assert "[Filtered]" in cleaned


_TIMING_UNITS = {
    "unclosed_value": '{"cookie":"' + "x" * 200_000,
    "backslashes": '{"cookie":"' + "\\" * 200_000,
    "bounded_key": '"' + "x" * 129 + '":"safe" ',
    "quoted_runs": '"cookie":"safe" ',
    "safe_objects": '{"cookie_count":2,"errorcode":5} ',
    "header": "Cookie: " + "x" * 200_000,
    "nested_safe_values": '''{"body":"retry 'token':'safe'"} ''',
    "nested_malformed_values": '''{"token":"ctx {'note':'he"ad safe', 'detail':'tail'}"} ''',
    "open_containers": '{"token":"ctx ' + "{" * 200_000,
}


def _best_json_scrub_seconds(unit, size):
    text = (unit * (size // len(unit) + 1))[:size]
    count = 200_000 // size
    best = float("inf")
    for _ in range(3):
        started = time.thread_time()
        for _ in range(count):
            assert isinstance(monitoring._scrub_string(text), str)
        elapsed = time.thread_time() - started
        assert elapsed < 10.0, f"{elapsed:.2f}s for {count} x {size} characters"
        best = min(best, elapsed / count)
    return best


@pytest.mark.parametrize("unit", _TIMING_UNITS.values(), ids=_TIMING_UNITS)
def test_json_and_header_scrubbing_stays_linear(unit):
    small = _best_json_scrub_seconds(unit, 50_000)
    large = _best_json_scrub_seconds(unit, 200_000)
    assert large <= 8 * small + 0.05, (small, large)
