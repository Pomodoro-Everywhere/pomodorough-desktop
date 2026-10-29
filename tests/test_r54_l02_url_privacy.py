"""Final-hook URL privacy regressions; synthetic canaries, no SDK or network."""

import json
import time

import pytest

from pomodorough.sentry_monitoring import scrub_sentry_breadcrumb, scrub_sentry_event


@pytest.mark.parametrize("parameter", [
    "token=URLCanaryMaple", "%74oken=URLCanaryMaple",
    "%2574oken=URLCanaryMaple", "%252574oken=URLCanaryMaple",
    "%" + "25" * 100 + "74oken=URLCanaryMaple",
    "%FFtoken=URLCanaryMaple", "%C0%AFtoken=URLCanaryMaple",
    "%ZZtoken=URLCanaryMaple", "%token=URLCanaryMaple",
    "to+ken=URLCanaryMaple", "to%00ken=URLCanaryMaple",
    "%74oken%3DURLCanaryMaple", "next=ok%26token%3DURLCanaryMaple",
    "token=URLCanaryMaple%23more", "safe%253Dtoken=URLCanaryMaple",
])
@pytest.mark.parametrize("separator", ["?", "#", ";", "?page=2&", "?page=2;"])
def test_final_hooks_remove_encoded_url_parameters(parameter, separator):
    url = f"https://example.invalid/callback{separator}{parameter}&next=1"
    crumb = {"category": "http", "message": f"GET {url}", "data": {"url": url}}
    event = {"message": f"disk unavailable: GET {url}", "request": {"url": url},
             "extra": {"detail": url}, "breadcrumbs": {"values": [crumb]}}
    for cleaned in (scrub_sentry_event(event), scrub_sentry_breadcrumb(crumb)):
        assert isinstance(cleaned, dict)
        rendered = json.dumps(cleaned)
        assert "URLCanaryMaple" not in rendered
        assert "[Filtered]" in rendered
        assert "next=1" in rendered
        assert "https://example.invalid/callback" in rendered
    assert "disk unavailable" in json.dumps(scrub_sentry_event(event))


@pytest.mark.parametrize("url", [
    "https://example.invalid/cb%3Ftoken%3DURLCanaryMaple",
    "https%3A%2F%2Fexample.invalid%2Fcb%3Ftoken%3DURLCanaryMaple",
    "https%253A%252F%252Fexample.invalid%253Ftoken%253DURLCanaryMaple",
    "https://example.invalid/cb?next=1%23token=URLCanaryMaple",
    "https://example.invalid/cb#token=URLCanaryMaple#page=2",
    "%3Ftoken%3DURLCanaryMaple", "%2523%2574oken=URLCanaryMaple",
    "/callback%3Ftoken%3DURLCanaryMaple",
    "/callback%%33%46token%%33%44URLCanaryMaple",
    "/callback%3%46token%3%44URLCanaryMaple",
    "https%3A%2F%2Fexample.invalid%3Ftoken%3Dpre URLCanaryMaple",
    "https://example.invalid/my%20file URLCanaryMaple",
    "https://example.invalid/cb?token=pre URLCanaryMaple",
])
def test_ambiguous_url_text_never_returns_original(url):
    for hook in (scrub_sentry_event, scrub_sentry_breadcrumb):
        cleaned = hook({"message": f"disk unavailable: {url} retry pending"})
        assert isinstance(cleaned, dict)
        rendered = json.dumps(cleaned)
        assert "URLCanaryMaple" not in rendered
        assert "disk unavailable" in rendered
        # Fail closed: prose after an ambiguous part may hold the secret's tail.
        assert "retry pending" not in rendered


@pytest.mark.parametrize("text", [
    "disk unavailable", "HTTP statuscode=503 retry pending",
    "GET https://example.invalid/callback?next=1;page=2#section=help",
    "GET https://example.invalid/callback?errorcode=503&statuscode=502",
    "GET https://example.invalid/callback?next=1 now", "failed; retrying in 5s",
    "Tom & Jerry", "error #5 occurred", "disk 95% full",
])
def test_safe_static_diagnostics_survive_both_final_hooks(text):
    for hook in (scrub_sentry_event, scrub_sentry_breadcrumb):
        assert hook({"message": text}) == {"message": text}


@pytest.mark.parametrize("parameter", [
    "%74oken=prefix'IndependentCanaryBirch",
    "%74oken='IndependentCanaryBirch",
    "%2574oken=prefix'IndependentCanaryBirch",
    "%FFtoken=prefix'IndependentCanaryBirch",
    "%74o\tken=IndependentCanaryBirch",
    '%74oken=prefix"IndependentCanaryBirch',
    "%74oken=prefix<IndependentCanaryBirch>",
    "%74oken=prefix\tIndependentCanaryBirch",
    "%74o\nken=IndependentCanaryBirch",
    "%74o\rken=IndependentCanaryBirch",
    "token=prefix'IndependentCanaryBirch",
])
@pytest.mark.parametrize("separator", ["?", "#", ";", "?page=2&"])
def test_final_hooks_filter_complete_ambiguous_token(parameter, separator):
    url = f"https://example.invalid/{separator}{parameter}&next=1"
    message = f"disk unavailable: GET {url} retry pending"
    crumb = {"category": "http", "message": message, "data": {"url": url}}
    event = {"message": message, "request": {"url": url},
             "extra": {"detail": url}, "breadcrumbs": {"values": [crumb]}}
    for cleaned in (scrub_sentry_event(event), scrub_sentry_breadcrumb(crumb)):
        assert isinstance(cleaned, dict)
        rendered = json.dumps(cleaned)
        assert "IndependentCanaryBirch" not in rendered
        assert "disk unavailable" in rendered
        assert "retry pending" in rendered
        assert "next=1" in rendered


_MALFORMED_PARAMETERS = (
    # Apostrophes and quotes inside literal and encoded-key values.
    "token=pre'MalformedCanaryCedar", "token='MalformedCanaryCedar'",
    "%74oken=pre'MalformedCanaryCedar", "%2574oken=a'b MalformedCanaryCedar",
    # Whitespace and controls that parsers strip from or keep in a key.
    "to ken=MalformedCanaryCedar", "to\tken=MalformedCanaryCedar",
    "to\nken=MalformedCanaryCedar", "to\rken=MalformedCanaryCedar",
    "to\x0bken=MalformedCanaryCedar", "to\x00ken=MalformedCanaryCedar",
    "to\x1fken=MalformedCanaryCedar", "to\x7fken=MalformedCanaryCedar",
    "to\xa0ken=MalformedCanaryCedar", " token=MalformedCanaryCedar",
    "\ttoken=MalformedCanaryCedar", "%74o ken=MalformedCanaryCedar",
    # Whitespace, controls, and structural characters after a sensitive value.
    "token=pre MalformedCanaryCedar", "token=pre\x0bMalformedCanaryCedar",
    "token=a\tb\nMalformedCanaryCedar", "%74oken=pre MalformedCanaryCedar",
    "token=abc;MalformedCanaryCedar", "token=abc?MalformedCanaryCedar",
    "token=abc#MalformedCanaryCedar",
    # Nested and mixed-case escapes.
    "%2574OKEN=MalformedCanaryCedar", "%74O%4BEN=MalformedCanaryCedar",
    "%2Fx%3Ftoken%3DMalformedCanaryCedar", "%%37%34oken=MalformedCanaryCedar",
    # `+` in keys and values.
    "to+ken=MalformedCanaryCedar", "+token=MalformedCanaryCedar",
    "token=a+b MalformedCanaryCedar",
    # `;`, `?`, and `#` inside keys that WHATWG keeps whole.
    "xtoken;y=MalformedCanaryCedar", "token;=MalformedCanaryCedar",
    "token?=MalformedCanaryCedar", "token#=MalformedCanaryCedar",
    "x'token=MalformedCanaryCedar", "page=2;token=MalformedCanaryCedar",
)


@pytest.mark.parametrize("parameter", _MALFORMED_PARAMETERS)
@pytest.mark.parametrize(
    "separator", ["?", "#", ";", "?page=2&", "?page=2;", "#a=1&"]
)
def test_final_hooks_filter_malformed_url_parameters(parameter, separator):
    url = f"https://example.invalid/callback{separator}{parameter}&next=1"
    message = f"disk unavailable: GET {url}"
    crumb = {"category": "http", "message": message, "data": {"url": url}}
    event = {"message": message, "request": {"url": url},
             "extra": {"detail": url}, "breadcrumbs": {"values": [crumb]}}
    for cleaned in (scrub_sentry_event(event), scrub_sentry_breadcrumb(crumb)):
        assert isinstance(cleaned, dict)
        rendered = json.dumps(cleaned)
        assert "MalformedCanaryCedar" not in rendered
        assert "[Filtered]" in rendered
        assert "disk unavailable" in rendered
        assert "https://example.invalid/callback" in rendered
        assert "next=1" in rendered


@pytest.mark.parametrize(("text", "expected"), [
    ("GET https://example.invalid/my%20file failed",
     "GET https://example.invalid/my[Filtered]"),
    ("GET https://example.invalid/cb?%74oken=x&next=1",
     "GET https://example.invalid/cb?[Filtered]&next=1"),
    ("GET https://example.invalid/cb?page=2;token=x;more&next=1",
     "GET https://example.invalid/cb?page=2;token=[Filtered]&next=1"),
    ("GET https://example.invalid/cb#token=x#page=2&next=1",
     "GET https://example.invalid/cb#token=[Filtered]&next=1"),
    ("GET https://example.invalid/cb?next=1;to\tken=x&page=2",
     "GET https://example.invalid/cb?next=1;[Filtered]&page=2"),
    ("GET https://example.invalid/cb?token=x retry pending",
     "GET https://example.invalid/cb?token=[Filtered]"),
])
def test_final_hooks_keep_unambiguous_url_neighbors(text, expected):
    for hook in (scrub_sentry_event, scrub_sentry_breadcrumb):
        assert hook({"message": text}) == {"message": expected}


_HOST = "https://example.invalid"
_BS = "\\"


def _assert_both_hooks_return(text, expected):
    for hook in (scrub_sentry_event, scrub_sentry_breadcrumb):
        assert hook({"message": text}) == {"message": expected}


@pytest.mark.parametrize(("text", "expected"), [
    ("https://alice:UserinfoCanaryAsh%21x@example.invalid/",
     "https://[Filtered]@example.invalid/"),
    ("https://alice:UserinfoCanaryAsh!x@example.invalid/",
     "https://[Filtered]@example.invalid/"),
    ("https://alice:UserinfoCanaryAsh+/=@example.invalid/",
     "https://[Filtered]@example.invalid/"),
    ("postgresql://app:Userinfo/CanaryAsh@db.example.invalid/pomodorough",
     "postgresql://[Filtered]@db.example.invalid/pomodorough"),
    ("https://alice:p@ss@UserinfoCanaryAsh@example.invalid/x",
     "https://[Filtered]@example.invalid/x"),
    ("https://al ice:Userinfo CanaryAsh@localhost/",
     "https://[Filtered]@localhost/"),
    ("https://alice:UserinfoCanaryAsh@localhost:8080/",
     "https://[Filtered]@localhost:8080/"),
    ("https://alice:UserinfoCanaryAsh@10.0.0.1/",
     "https://[Filtered]@[Filtered]/"),
    ("https://alice:UserinfoCanaryAsh@[::1]/",
     "https://[Filtered]@[[Filtered]]/"),
    ("http://alice:UserinfoCanaryAsh%40x@proxy.example.invalid:3128",
     "http://[Filtered]@proxy.example.invalid:3128"),
    ("redis://:UserinfoCanaryAsh@cache.example.invalid:6379",
     "redis://[Filtered]@cache.example.invalid:6379"),
    ("https:\\/\\/alice:UserinfoCanaryAsh@localhost\\/",
     "https:\\/\\/[Filtered]@localhost\\/"),
    ("https:%2F%2Falice:UserinfoCanaryAsh@localhost/", "https:[Filtered]"),
    ("https://alice:UserinfoCanaryAsh@localhost/?next=1",
     "https://[Filtered]@localhost/?next=1"),
])
def test_final_hooks_filter_userinfo_in_any_encoding(text, expected):
    _assert_both_hooks_return(text, expected)


@pytest.mark.parametrize(("text", "expected"), [
    (f"{_HOST}/u/EmailCanaryElm%2Bwork@corp.example", f"{_HOST}/u/[Filtered]"),
    (f"{_HOST}/cb?a=b EmailCanaryElm%3Bx@corp.example",
     f"{_HOST}/cb?a=b [Filtered]"),
    ("ok@corp.example+EmailCanaryElm@corp.example", "[Filtered][Filtered]"),
    ("mail EmailCanaryElm%21x@corp.example failed", "mail [Filtered] failed"),
    ("mail EmailCanaryElm!x@corp.example failed", "mail [Filtered] failed"),
    ("mail o'EmailCanaryElm@corp.example failed", "mail [Filtered] failed"),
])
def test_final_hooks_filter_whole_email_before_url_rules(text, expected):
    _assert_both_hooks_return(text, expected)


@pytest.mark.parametrize(("field", "value", "expected"), [
    ("http.query", "token=QueryCanaryWillow&next=1", "token=[Filtered]&next=1"),
    ("http.query", "%74oken=QueryCanaryWillow&next=1", "[Filtered]&next=1"),
    ("http.query", "%2574oken=QueryCanaryWillow&next=1", "[Filtered]&next=1"),
    ("http.query", "to ken=QueryCanaryWillow&next=1", "[Filtered]&next=1"),
    ("http.query", "code=QueryCanaryWillow&state=s",
     "code=[Filtered]&state=[Filtered]"),
    ("http.query", b"token=QueryCanaryWillow&next=1", "token=[Filtered]&next=1"),
    ("http.query", bytes([0xFF]) + b"&token=QueryCanaryWillow",
     "\N{REPLACEMENT CHARACTER}&token=[Filtered]"),
    ("http.fragment", "access_token=QueryCanaryWillow&token_type=bearer",
     "access_token=[Filtered]&token_type=[Filtered]"),
    ("http.query", "", ""),
    ("http.query", "next=1&page=2", "next=1&page=2"),
    ("http.fragment", "page=2", "page=2"),
])
def test_breadcrumb_query_fields_scrub_as_url_parts(field, value, expected):
    data = {"url": f"{_HOST}/cb", "http.method": "GET", field: value}
    crumb = {"category": "httplib", "data": data}
    event = scrub_sentry_event({"breadcrumbs": {"values": [crumb]}})
    expected_data = {"url": f"{_HOST}/cb", "http.method": "GET", field: expected}
    for cleaned in (scrub_sentry_breadcrumb(crumb), event["breadcrumbs"]["values"][0]):
        assert cleaned == {"category": "httplib", "data": expected_data}


@pytest.mark.parametrize(("query", "expected"), [
    ("%74oken=QueryCanaryWillow&next=1", "[Filtered]&next=1"),
    ("code=QueryCanaryWillow", "code=[Filtered]"),
    ([["%74oken", "QueryCanaryWillow"], ["next", "1"]],
     [["%74oken", "[Filtered]"], ["next", "1"]]),
    ((("%74oken", "QueryCanaryWillow"), ("next", "1")),
     (("%74oken", "[Filtered]"), ("next", "1"))),
    ({"%74oken": "QueryCanaryWillow", "next": "1"},
     {"%74oken": "[Filtered]", "next": "1"}),
    ([["to ken", "QueryCanaryWillow"], ["page", "2"]],
     [["to ken", "[Filtered]"], ["page", "2"]]),
    ([["sig", "QueryCanaryWillow"]], [["sig", "[Filtered]"]]),
    ([["next", "a%26token%3DQueryCanaryWillow"]], [["next", "a%26[Filtered]"]]),
    ([[1, "QueryCanaryWillow"]], [[1, "[Filtered]"]]),
    ([["QueryCanaryWillow@corp.example", "1"]], [["[Filtered]", "[Filtered]"]]),
    (["token", "QueryCanaryWillow"], ["[Filtered]", "[Filtered]"]),
    ([["a", "b", "QueryCanaryWillow"]], ["[Filtered]"]),
    ([["next", "1"], ["page", "2"]], [["next", "1"], ["page", "2"]]),
    ("", ""),
])
def test_event_request_query_string_scrubs_every_sentry_form(query, expected):
    request = {"url": f"{_HOST}/cb", "method": "GET", "query_string": query}
    cleaned = scrub_sentry_event({"request": request})
    assert cleaned == {"request": {"url": f"{_HOST}/cb", "method": "GET",
                                   "query_string": expected}}


@pytest.mark.parametrize("query", [None, 7, 2.5, True])
def test_event_request_query_string_keeps_json_primitives(query):
    event = {"request": {"query_string": query}}
    assert scrub_sentry_event(event) == event


def test_query_fields_share_the_generic_depth_limit():
    seen = set()
    for depth in range(25):
        nested = {"http.query": "page=2", "note": "page=2"}
        for _ in range(depth):
            nested = {"child": nested}
        cleaned = scrub_sentry_breadcrumb({"data": nested})["data"]
        while isinstance(cleaned, dict) and "child" in cleaned:
            cleaned = cleaned["child"]
        if isinstance(cleaned, dict):
            assert cleaned["http.query"] == cleaned["note"]
            seen.add(cleaned["note"])
    assert seen == {"page=2", "[Filtered]"}


@pytest.mark.parametrize(("text", "expected"), [
    (f"?a=1{_BS}u0026token=EscapeCanaryOak&next=1",
     f"?a=1{_BS}u0026[Filtered]&next=1"),
    (f'{{"url":"{_HOST}/cb{_BS}u003ftoken=EscapeCanaryOak"}}',
     f'{{"url":"{_HOST}/cb{_BS}u003f[Filtered]'),
    (f"cb{_BS}u003Ftoken=EscapeCanaryOak", f"cb{_BS}u003F[Filtered]"),
    (f"cb{_BS}u0023token=EscapeCanaryOak", f"cb{_BS}u0023[Filtered]"),
    (f"cb{_BS}u0025%74oken=EscapeCanaryOak", f"cb{_BS}u0025[Filtered]"),
    (f"cb{_BS * 2}u003ftoken=EscapeCanaryOak", f"cb{_BS * 2}u003f[Filtered]"),
    (f"cb{_BS}x26token=EscapeCanaryOak", f"cb{_BS}x26[Filtered]"),
    ("/cb%u003Ftoken=EscapeCanaryOak", "/cb%u[Filtered]"),
    ("/cb%U0026token=EscapeCanaryOak", "/cb%U[Filtered]"),
    ("/cb%3\tFtoken=EscapeCanaryOak", "/cb%3\t[Filtered]"),
    ("/cb%\n3Ftoken=EscapeCanaryOak", "/cb%\n[Filtered]"),
    ("/cb%25\r3Ftoken=EscapeCanaryOak", "/cb%25\r[Filtered]"),
    (f"/cb%3{_BS}tFtoken=EscapeCanaryOak", f"/cb%3{_BS}t[Filtered]"),
    (f"/cb%3{_BS}u0009Ftoken=EscapeCanaryOak", f"/cb%3{_BS}u0009[Filtered]"),
])
def test_final_hooks_treat_hidden_separator_escapes_as_encoded(text, expected):
    _assert_both_hooks_return(text, expected)


@pytest.mark.parametrize(("text", "expected"), [
    ("/cb%3Fa=1 EncodedCanaryYew&next=1", "/cb%3F[Filtered]&next=1"),
    ("see cb%2523a=1 EncodedCanaryYew", "see cb%2523[Filtered]"),
    (f"cb{_BS}u003fa=1 EncodedCanaryYew", f"cb{_BS}u003f[Filtered]"),
    ("/cb%3Fpage=2&next=1", "/cb%3F[Filtered]&next=1"),
])
def test_final_hooks_filter_everything_after_an_encoded_separator(text, expected):
    _assert_both_hooks_return(text, expected)


@pytest.mark.parametrize(("text", "expected"), [
    (f"{_HOST}/cb%C0%BFtoken=HeadCanaryFir&next=1", f"{_HOST}/cb[Filtered]&next=1"),
    (f"{_HOST}/cb%EF%BC%9Ftoken=HeadCanaryFir&next=1",
     f"{_HOST}/cb[Filtered]&next=1"),
    ("https%3A%2F%2Fexample.invalid%2Fcb%C0%BFtoken%3DHeadCanaryFir",
     "https[Filtered]"),
    ("https:%2F%2Fexample.invalid%2Fcb%C0%BFtoken%3DHeadCanaryFir",
     "https:[Filtered]"),
    (f"{_HOST}/p%61th HeadCanaryFir", f"{_HOST}/p[Filtered]"),
    (f"{_HOST}/cb?next=1", f"{_HOST}/cb?next=1"),
])
def test_final_hooks_keep_only_the_literal_url_head_before_an_escape(
    text, expected
):
    _assert_both_hooks_return(text, expected)


@pytest.mark.parametrize(("text", "expected"), [
    ("/cb%253Ftoken%253DDepthCanaryPine", "/cb%253F[Filtered]"),
    ("/cb%25253Ftoken=DepthCanaryPine", "/cb%25253F[Filtered]"),
    ("see cb%2523access_token=DepthCanaryPine", "see cb%2523[Filtered]"),
    ("/cb%253Fa%253D1%2526token%253DDepthCanaryPine&next=1",
     "/cb%253F[Filtered]&next=1"),
    ("/cb%25%33%46token=DepthCanaryPine", "/cb%25%[Filtered]"),
    ("/cb%2525u003Ftoken=DepthCanaryPine", "/cb%2525u[Filtered]"),
])
def test_final_hooks_filter_nested_encoded_separators(text, expected):
    _assert_both_hooks_return(text, expected)


@pytest.mark.parametrize(("query", "expected"), [
    ("X-Amz-Signature=SignatureCanaryBirch&X-Amz-Date=20261028",
     "X-Amz-Signature=[Filtered]&X-Amz-Date=20261028"),
    ("x-amz-signature=SignatureCanaryBirch&x-amz-date=20261028",
     "x-amz-signature=[Filtered]&x-amz-date=20261028"),
    ("sv=2024&sig=SignatureCanaryBirch&se=2026", "sv=2024&sig=[Filtered]&se=2026"),
    ("sv=2024&SIG=SignatureCanaryBirch&se=2026", "sv=2024&SIG=[Filtered]&se=2026"),
    ("Signature=SignatureCanaryBirch&Expires=1", "Signature=[Filtered]&Expires=1"),
    ("X-Goog-Signature=SignatureCanaryBirch&X-Goog-Date=1",
     "X-Goog-Signature=[Filtered]&X-Goog-Date=1"),
    ("X-Amz-%53ignature=SignatureCanaryBirch&next=1", "[Filtered]&next=1"),
    ("signal=ok&design=ok", "signal=ok&design=ok"),
])
def test_final_hooks_filter_signed_url_signatures(query, expected):
    _assert_both_hooks_return(f"{_HOST}/o?{query}", f"{_HOST}/o?{expected}")


def test_final_hooks_filter_signature_keys_in_mappings():
    canary = "SignatureCanaryBirch"
    keys = ("signature", "X-Amz-Signature", "Signatures", "sig", "SIG")
    data = {**{key: canary for key in keys}, "signal": "ok", "design": "ok"}
    expected = {**{key: "[Filtered]" for key in keys}, "signal": "ok", "design": "ok"}
    assert scrub_sentry_event({"extra": data}) == {"extra": expected}
    crumb = {"category": "http", "data": data}
    assert scrub_sentry_breadcrumb(crumb) == {"category": "http", "data": expected}


_ADVERSARIAL_UNITS = {
    "scheme_separator": "a://", "percent": "%", "ampersand": "&",
    "semicolon": ";", "word": "a", "dotted": "a.", "at_dotted": "@a.",
    "userinfo_at": "://a@", "userinfo_colon": "://a:",
    "json_userinfo_colon": "a:\\/\\/b:", "backslash": "\\",
    "nested_percent": "%25", "email_atext": "a!",
}
_ADVERSARIAL_SHAPES = sorted(
    [*_ADVERSARIAL_UNITS, "email_domain", "dotted_host", "json_url_list"]
)
# A linear scan grows about 4x from n to 4n and a quadratic one about 16x. Host
# load slows both sizes alike, so the ratio holds where an absolute cap flaked.
# Thread CPU time leaves out time spent waiting for a core. The 50k text runs 4
# times per timing so both sizes hold the CPU equally long: the scheduler can
# move a long-running thread to a slower core, and equal run lengths expose both
# sizes to that alike. The slack keeps timer noise on millisecond shapes from
# deciding the result.
_SCALE_SIZES = (50_000, 200_000)
_SCALE_RATIO_LIMIT = 8
_SCALE_SLACK_SECONDS = 0.05
# Hang guard only. The quadratic regressions this pins use 9-12 s of thread CPU
# at 200k; one that stays under the guard fails the ratio check instead.
_SCRUB_HANG_SECONDS = 10.0


def _adversarial_text(shape, size):
    if shape == "email_domain":
        return "x@" + "a." * (size // 2)
    if shape == "dotted_host":
        return "https://" + "a." * (size // 2)
    if shape == "json_url_list":
        return json.dumps([f"{_HOST}/cb?a=1&b=%2"] * (size // 40))
    unit = _ADVERSARIAL_UNITS[shape]
    return unit * (size // len(unit))


def _scrub_cpu_seconds(text, count):
    """Return thread CPU seconds per breadcrumb scrub over `count` scrubs."""
    started = time.thread_time()
    for _ in range(count):
        assert isinstance(scrub_sentry_breadcrumb({"message": text}), dict)
    elapsed = time.thread_time() - started
    assert elapsed < _SCRUB_HANG_SECONDS, (
        f"{elapsed:.2f}s for {count} x {len(text)} characters")
    return elapsed / count


def _best_scrub_seconds(shape):
    """Return the fastest per-scrub seconds at n and 4n over three rounds."""
    texts = {size: _adversarial_text(shape, size) for size in _SCALE_SIZES}
    best = dict.fromkeys(_SCALE_SIZES, float("inf"))
    for _ in range(3):
        for size, text in texts.items():
            count = _SCALE_SIZES[-1] // size
            best[size] = min(best[size], _scrub_cpu_seconds(text, count))
    return tuple(best.values())


@pytest.mark.parametrize("shape", _ADVERSARIAL_SHAPES)
def test_final_hooks_scrub_adversarial_text_in_linear_time(shape):
    small, large = _best_scrub_seconds(shape)
    assert large <= _SCALE_RATIO_LIMIT * small + _SCALE_SLACK_SECONDS, (
        f"{shape}: {small:.3f}s at {_SCALE_SIZES[0]} characters, "
        f"{large:.3f}s at {_SCALE_SIZES[1]}")
