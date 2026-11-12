"""Final-hook diagnostic key privacy regressions; synthetic canaries, no SDK
initialization or network.

The SDK serializes an event before `before_send` runs, so the final event is
`serialize`, then `scrub_sentry_event`, then the envelope's JSON encoding.
Breadcrumbs pass `scrub_sentry_breadcrumb` first, unserialized.
"""

import json

import pytest
from sentry_sdk.serializer import serialize
from sentry_sdk.utils import AnnotatedValue, json_dumps

from pomodorough.sentry_monitoring import scrub_sentry_breadcrumb, scrub_sentry_event

_CANARY = "KeyCanaryHazel"
_BS = "\\"
_FILTERED = "[Filtered]"


class _DisplayedKey:
    """A non-string key whose text form names a person."""

    def __str__(self):
        return f"{_CANARY}@example.test"

    __repr__ = __str__


class _ShadowedKey(str):
    """A str whose content is safe but whose `__str__` is not."""

    def __str__(self):
        return f"{_CANARY}@example.test"


_TEXT_KEYS = {
    "email": f"{_CANARY}@example.test",
    "percent_email": f"{_CANARY}%40example.test",
    "percent_name": f"%4B{_CANARY}",
    "double_percent": f"{_CANARY}%2540x",
    "tab_inside": f"{_CANARY}\tnote",
    "tab_leading": f"\t{_CANARY}",
    "tab_trailing": f"{_CANARY}\t",
    "newline": f"{_CANARY}\nnote",
    "carriage_return": f"{_CANARY}\rnote",
    "nul": f"{_CANARY}\x00note",
    "escape_control": f"{_CANARY}\x1bnote",
    "delete_control": f"{_CANARY}\x7f",
    "space_title": f"Call {_CANARY} tomorrow",
    "trailing_space": f"{_CANARY} ",
    "zero_width": f"{_CANARY}\N{ZERO WIDTH SPACE}",
    "fullwidth_at": f"{_CANARY}\N{FULLWIDTH COMMERCIAL AT}example.test",
    "accented": f"caf\N{LATIN SMALL LETTER E WITH ACUTE}{_CANARY}",
    "json_escape": f"{_CANARY}{_BS}u0040example.test",
    "c_escape": f"{_CANARY}{_BS}tnote",
    "posix_path": f"/Users/{_CANARY}/notes",
    "windows_path": f"C:{_BS}Users{_BS}{_CANARY}",
    "url": f"https://example.invalid/?{_CANARY}=1",
    "colon": f"{_CANARY}:secret",
    "quoted": f'"{_CANARY}"',
    "bracketed": f"[{_CANARY}]",
    "placeholder_lookalike": f"[Filtered key 1]{_CANARY}",
    "leading_digit": f"1{_CANARY}",
    "leading_dash": f"-{_CANARY}",
    "leading_dot": f".{_CANARY}",
    "invite": f"pomodorough1.{_CANARY}",
    "invite_upper": f"POMODOROUGH1{_CANARY}",
    "dotted_quad": f"peer-10.0.0.1-{_CANARY}",
    "short_dotted_quad": f"{_CANARY}_1.2.3.4",
    "six_digits": f"{_CANARY}_123456",
    "uuid": f"{_CANARY}-123e4567-e89b-12d3-a456-426614174000",
    "opaque_run": f"{_CANARY}0a1b",
    "length_65": _CANARY + "x" * (65 - len(_CANARY)),
    "very_long": _CANARY * 1_000,
}
_OBJECT_KEYS = {
    "bytes": f"{_CANARY}@example.test".encode(),
    "tuple": (_CANARY, "note"),
    "frozenset": frozenset({_CANARY}),
    "object": _DisplayedKey(),
    "str_subclass": _ShadowedKey("plain"),
}
_UNSAFE_KEYS = {**_TEXT_KEYS, **_OBJECT_KEYS}


def _in_extra(key, value):
    return {"extra": {key: value, "detail": "disk unavailable"}}


def _in_tags(key, value):
    return {"tags": {key: value, "component": "sync"}}


def _in_context_name(key, value):
    return {"contexts": {key: {"state": value}, "os": {"name": "macOS"}}}


def _in_context_field(key, value):
    return {"contexts": {"sync": {key: value, "phase": "pull"}}}


def _in_nested_map(key, value):
    return {"extra": {"outer": {"middle": {key: value, "phase": "pull"}}}}


def _in_listed_map(key, value):
    return {"extra": {"attempts": [{"phase": "pull"}, {key: value}]}}


def _in_frame_vars(key, value):
    frame = {"function": "pull", "vars": {key: value, "attempt": 2}}
    return {"exception": {"values": [{"type": "OSError",
                                      "stacktrace": {"frames": [frame]}}]}}


def _in_request_headers(key, value):
    return {"request": {"headers": {key: value, "Accept": "application/json"}}}


def _in_event_root(key, value):
    return {key: value, "level": "error"}


def _in_event_breadcrumb(key, value):
    crumb = {"category": "sync", "data": {key: value, "phase": "pull"}}
    return {"breadcrumbs": {"values": [crumb]}}


_EVENT_BOUNDARIES = {
    "extra": _in_extra, "tags": _in_tags, "context_name": _in_context_name,
    "context_field": _in_context_field, "nested_map": _in_nested_map,
    "listed_map": _in_listed_map, "frame_vars": _in_frame_vars,
    "request_headers": _in_request_headers, "event_root": _in_event_root,
    "event_breadcrumb": _in_event_breadcrumb,
}


def _final_event(event):
    cleaned = scrub_sentry_event(serialize(event))
    assert isinstance(cleaned, dict)
    return cleaned, json_dumps(cleaned).decode()


def _final_breadcrumb(crumb):
    cleaned = scrub_sentry_breadcrumb(crumb)
    assert isinstance(cleaned, dict)
    event, rendered = _final_event({"breadcrumbs": {"values": [cleaned]}})
    return cleaned, event["breadcrumbs"]["values"][0], rendered


def _assert_no_canary(*renderings):
    for rendering in renderings:
        assert _CANARY.lower() not in rendering.lower()


def _filtered_entries(mapping):
    return {key: item for key, item in mapping.items() if key.startswith("[")}


@pytest.mark.parametrize("key", list(_UNSAFE_KEYS.values()), ids=list(_UNSAFE_KEYS))
@pytest.mark.parametrize("boundary", list(_EVENT_BOUNDARIES))
def test_final_event_replaces_private_key_names(boundary, key):
    event = _EVENT_BOUNDARIES[boundary](key, f"value {_CANARY}")
    cleaned, rendered = _final_event(event)
    _assert_no_canary(rendered, repr(cleaned))
    assert '"[Filtered key 1]":"[Filtered]"' in rendered
    assert all(type(name) is str for name in _keys_of(cleaned))


@pytest.mark.parametrize("key", list(_UNSAFE_KEYS.values()), ids=list(_UNSAFE_KEYS))
def test_final_event_hook_replaces_raw_private_keys(key):
    # Defense in depth: the hook also holds when an event skips serialization.
    for boundary in _EVENT_BOUNDARIES.values():
        cleaned = scrub_sentry_event(boundary(key, f"value {_CANARY}"))
        assert isinstance(cleaned, dict)
        rendered = json_dumps(cleaned).decode()
        _assert_no_canary(rendered, repr(cleaned))
        assert all(type(name) is str for name in _keys_of(cleaned))


@pytest.mark.parametrize("key", list(_UNSAFE_KEYS.values()), ids=list(_UNSAFE_KEYS))
def test_final_breadcrumb_replaces_private_data_keys(key):
    crumb = {"category": "sync", "message": "pull failed",
             "data": {"phase": "pull", key: f"value {_CANARY}", "attempt": 2}}
    hooked, final, rendered = _final_breadcrumb(crumb)
    _assert_no_canary(rendered, repr(hooked))
    expected = {"phase": "pull", "[Filtered key 1]": _FILTERED, "attempt": 2}
    assert hooked == {"category": "sync", "message": "pull failed", "data": expected}
    assert final == hooked


def _query_forms(key):
    value = f"value {_CANARY}"
    return ({key: value, "next": "1"}, [[key, value], ["next", "1"]],
            ((key, value), ("next", "1")))


@pytest.mark.parametrize("key", list(_UNSAFE_KEYS.values()), ids=list(_UNSAFE_KEYS))
def test_query_forms_replace_private_parameter_names(key):
    for query in _query_forms(key):
        request = {"url": "https://example.invalid/cb", "query_string": query}
        crumb = {"category": "httplib", "data": {"http.query": query}}
        for cleaned in (scrub_sentry_event({"request": request}),
                        scrub_sentry_breadcrumb(crumb),
                        _final_event({"request": request})[0],
                        _final_breadcrumb(crumb)[1]):
            rendered = json_dumps(cleaned).decode()
            _assert_no_canary(rendered, repr(cleaned))
            assert '"[Filtered key 1]"' in rendered
            assert '"next"' in rendered


def test_query_pair_collisions_number_per_list():
    query = [["a\tb", "1"], ["a b", "2"], ["next", "1"], ["a%09b", "3"], ["odd"]]
    cleaned = scrub_sentry_event({"request": {"query_string": query}})
    assert cleaned["request"]["query_string"] == [
        ["[Filtered key 1]", "[Filtered]"], ["[Filtered key 2]", "[Filtered]"],
        ["next", "1"], ["[Filtered key 3]", "[Filtered]"], "[Filtered]",
    ]


@pytest.mark.parametrize(("key", "word"), [
    ("Canary\tx", "Canary"), ("person%40example.test", "person"),
])
def test_query_key_canaries_never_reach_the_final_event(key, word):
    forms = ({key: "1", "next": "1"}, [[key, "1"], ["next", "1"]],
             ((key, "1"), ("next", "1")))
    for query in forms:
        request = {"url": "https://example.invalid/cb", "query_string": query}
        crumb = {"category": "httplib", "data": {"http.query": query}}
        event = _final_event({"request": request})[0]
        assert event["request"]["query_string"] == (
            {"[Filtered key 1]": _FILTERED, "next": "1"} if isinstance(query, dict)
            else [["[Filtered key 1]", _FILTERED], ["next", "1"]])
        for cleaned in (event, _final_breadcrumb(crumb)[1]):
            rendered = json_dumps(cleaned).decode()
            assert word.lower() not in rendered.lower()


@pytest.mark.parametrize("value", [_CANARY, {"inner": _CANARY}], ids=["text", "map"])
@pytest.mark.parametrize("parent", ["query_string", "http.query", "http.fragment"])
def test_query_dict_values_keep_the_canonical_key_rule(parent, value):
    # R54-L02: only a canonical query key names its value. `""` passes the
    # key policy, because `_meta` annotations use it, but it is not a
    # canonical query key, so its value stays filtered.
    query = {"": value, "page": "2"}
    if parent == "query_string":
        request = {"url": "https://example.invalid/cb", "query_string": query}
        hooked = scrub_sentry_event({"request": request})["request"]
        final, rendered = _final_event({"request": request})
        final = final["request"]
    else:
        hooked, final, rendered = _final_breadcrumb(
            {"category": "httplib", "data": {parent: query}})
        hooked, final = hooked["data"], final["data"]
    _assert_no_canary(rendered, repr(hooked))
    assert hooked[parent] == final[parent] == {"": _FILTERED, "page": "2"}


def _keys_of(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys_of(item)


@pytest.mark.parametrize("key", [_TEXT_KEYS["tab_inside"], _TEXT_KEYS["percent_email"]])
def test_private_key_value_is_filtered_whatever_its_shape(key):
    # The key cannot classify its own value: `to\tken` reads as `token`.
    for value in (_CANARY, {"inner": _CANARY}, [_CANARY], 7, None):
        cleaned, rendered = _final_event({"extra": {key: value}})
        assert cleaned == {"extra": {"[Filtered key 1]": _FILTERED}}
    hidden_token = scrub_sentry_event({"extra": {"to\tken": "plainvalue"}})
    assert hidden_token == {"extra": {"[Filtered key 1]": _FILTERED}}


def test_colliding_private_keys_never_overwrite_each_other():
    # A sanitizer that dropped or replaced the control character would map
    # all four raw keys to `note_a` or `notea`.
    raw = {"note\ta": f"{_CANARY}1", "note a": f"{_CANARY}2",
           "note\na": f"{_CANARY}3", "note%09a": f"{_CANARY}4", "phase": "pull"}
    for cleaned in (scrub_sentry_event({"extra": raw})["extra"],
                    scrub_sentry_breadcrumb({"data": raw})["data"]):
        assert cleaned == {
            "[Filtered key 1]": _FILTERED, "[Filtered key 2]": _FILTERED,
            "[Filtered key 3]": _FILTERED, "[Filtered key 4]": _FILTERED,
            "phase": "pull",
        }


def test_placeholder_lookalike_keys_cannot_claim_a_placeholder():
    raw = {"[Filtered key 2]": f"{_CANARY}1", "a\tb": f"{_CANARY}2",
           "[Filtered key 1]": f"{_CANARY}3"}
    cleaned = scrub_sentry_event({"tags": raw})["tags"]
    assert cleaned == {"[Filtered key 1]": _FILTERED, "[Filtered key 2]": _FILTERED,
                       "[Filtered key 3]": _FILTERED}


def test_non_string_keys_never_merge_with_their_text_form():
    raw = {1: f"{_CANARY}1", "1": "disk unavailable", None: f"{_CANARY}3",
           2.5: f"{_CANARY}4", b"x": f"{_CANARY}5", "count": 3}
    cleaned = scrub_sentry_event({"extra": raw})["extra"]
    assert cleaned == {"[Filtered key 1]": _FILTERED, "1": "disk unavailable",
                       "[Filtered key 2]": _FILTERED, "[Filtered key 3]": _FILTERED,
                       "[Filtered key 4]": _FILTERED, "count": 3}
    assert list(cleaned) == ["[Filtered key 1]", "1", "[Filtered key 2]",
                             "[Filtered key 3]", "[Filtered key 4]", "count"]


def test_placeholders_number_per_mapping():
    event = {"extra": {"a\tb": 1, "inner": {"c\td": 2, "e\tf": 3}},
             "tags": {"g\th": "x"}}
    assert scrub_sentry_event(event) == {
        "extra": {"[Filtered key 1]": _FILTERED,
                  "inner": {"[Filtered key 1]": _FILTERED, "[Filtered key 2]": _FILTERED}},
        "tags": {"[Filtered key 1]": _FILTERED},
    }


@pytest.mark.parametrize("imitation", [
    "level\t", " level", "level ", "le\tvel", "level\x00", "lev%65l", "level:",
    "level\N{ZERO WIDTH SPACE}", "\N{LATIN SMALL LETTER L WITH STROKE}evel",
])
def test_static_name_imitations_never_replace_the_static_value(imitation):
    event = {"level": "error", imitation: _CANARY, "tags": {"component": "sync"}}
    cleaned, rendered = _final_event(event)
    _assert_no_canary(rendered)
    assert cleaned == {"level": "error", "[Filtered key 1]": _FILTERED,
                       "tags": {"component": "sync"}}


def test_cloud_taxonomy_survives_an_imitating_tag():
    event = {"tags": {"cloud_credential.operation": "sync",
                      "cloud_credential.cause": "filesystem",
                      "cloud_credential.operation\t": _CANARY}}
    cleaned, rendered = _final_event(event)
    _assert_no_canary(rendered)
    assert cleaned["tags"] == {"cloud_credential.operation": "sync",
                               "cloud_credential.cause": "filesystem",
                               "[Filtered key 1]": _FILTERED}
    assert cleaned["contexts"] == {"cloud_credential": {"operation": "sync",
                                                        "cause": "filesystem"}}


def test_sdk_breadth_meta_never_spells_a_private_key():
    # The serializer records trimming in `_meta` under the raw key path.
    wide = {f"item{index}": index for index in range(12)}
    event = {"extra": {f"{_CANARY}\tnote": wide, "wide": wide}}
    cleaned, rendered = _final_event(event)
    _assert_no_canary(rendered)
    assert cleaned["extra"]["[Filtered key 1]"] == _FILTERED
    assert len(cleaned["extra"]["wide"]) == 10
    assert cleaned["_meta"] == {"extra": {"[Filtered key 1]": _FILTERED,
                                          "wide": {"": {"len": 12}}}}


def test_sdk_depth_repr_is_filtered_and_its_meta_survives():
    # Past its depth limit the serializer writes the repr of the rest, which
    # spells the deeper raw keys inside a string value.
    deep = {"a": {"b": {"c": {"d": {"e": {f"{_CANARY}\tleaf": 1}}}}}}
    crumb = {"category": "sync", "data": deep}
    cleaned, rendered = _final_event({"extra": deep, "breadcrumbs": {"values": [crumb]}})
    _assert_no_canary(rendered)
    assert cleaned["extra"] == {"a": {"b": {"c": {"d": {"e": _FILTERED}}}}}
    assert cleaned["breadcrumbs"]["values"][0]["data"] == {"a": {"b": {"c": {"d": _FILTERED}}}}
    limit = {"": {"rem": [["!limit", "x"]]}}
    assert cleaned["_meta"] == {
        "extra": {"a": {"b": {"c": {"d": {"e": limit}}}}},
        "breadcrumbs": {"values": {"0": {"data": {"a": {"b": {"c": {"d": limit}}}}}}},
    }


def test_depth_repr_filter_ignores_unrelated_meta():
    event = {"extra": {"note": "{'a': 1}"},
             "_meta": {"extra": {"note": {"": {"len": 9000}}, "gone": {"": {}}}}}
    assert scrub_sentry_event(event) == event


@pytest.mark.parametrize("removed", [
    AnnotatedValue.removed_because_raw_data,
    AnnotatedValue.removed_because_over_size_limit,
    AnnotatedValue.substituted_because_contains_sensitive_data,
], ids=["raw", "over_size", "sensitive"])
def test_depth_repr_filter_keeps_values_under_other_removals(removed):
    # The SDK also writes `!raw` and `!config` removals. Only `!limit` marks
    # a depth repr, so a value under any other removal stays as it was.
    event = serialize({"request": {"method": "POST", "data": removed()},
                       "extra": {"note": "disk unavailable"}})
    rule = event["_meta"]["request"]["data"][""]["rem"][0]
    assert rule[0] in ("!raw", "!config")
    event["_meta"]["extra"] = {"note": {"": {"rem": [rule]}}}
    assert scrub_sentry_event(event) == event


def test_very_deep_private_keys_stay_filtered():
    nested = {f"{_CANARY}\tleaf": _CANARY}
    for _ in range(30):
        nested = {"child": nested, f"{_CANARY}\tsibling": 1}
    cleaned, rendered = _final_event({"extra": nested})
    _assert_no_canary(rendered)


def _deep_databag(levels):
    # Safe names lead to the private keys, so with five or more levels every
    # private key sits below the SDK's five-level databag limit. `_CANARY`
    # alone is a valid diagnostic name; only the repr rule can remove it.
    bag = {f"{_CANARY}\tleaf": f"value {_CANARY}", _CANARY: 1}
    for level in reversed(range(levels)):
        bag = {f"level{level}": bag}
    return bag


def _frame_with_vars(bag):
    # The SDK serializes frame locals early, with `is_vars`, and adds no `_meta`.
    return {"function": "pull", "vars": serialize(bag, is_vars=True)}


_DATABAG_ROOTS = {
    "extra": (lambda bag: {"extra": bag}, ("extra",)),
    "request_data": (lambda bag: {"request": {"method": "POST", "data": bag}},
                     ("request", "data")),
    "breadcrumb": (lambda bag: {"breadcrumbs": {"values": [{"category": "sync", **bag}]}},
                   ("breadcrumbs", "values", 0)),
    "exception_vars": (
        lambda bag: {"exception": {"values": [
            {"type": "OSError", "stacktrace": {"frames": [_frame_with_vars(bag)]}}]}},
        ("exception", "values", 0, "stacktrace", "frames", 0, "vars")),
    "thread_vars": (
        lambda bag: {"threads": {"values": [
            {"id": 1, "stacktrace": {"frames": [_frame_with_vars(bag)]}}]}},
        ("threads", "values", 0, "stacktrace", "frames", 0, "vars")),
    "stacktrace_vars": (
        lambda bag: {"stacktrace": {"frames": [_frame_with_vars(bag)]}},
        ("stacktrace", "frames", 0, "vars")),
}
_TRIMMED_LEVEL = tuple(f"level{level}" for level in range(5))


def _node_at(node, path):
    for segment in path:
        node = node[segment]
    return node


def _with_meta(event, variant):
    if variant == "sdk":
        return event
    stripped = {key: item for key, item in event.items() if key != "_meta"}
    if variant == "missing":
        return stripped
    meta = json.dumps(event.get("_meta", {})).replace('"!limit"', '"!trimmed"')
    return {**stripped, "_meta": {} if variant == "empty" else json.loads(meta)}


@pytest.mark.parametrize("meta", ["sdk", "missing", "empty", "no_limit_mark"])
@pytest.mark.parametrize("levels", [5, 6, 9])
@pytest.mark.parametrize("root", list(_DATABAG_ROOTS))
def test_sdk_depth_repr_never_spells_a_deeper_key(root, levels, meta):
    # Fail closed: the repr is filtered by its position, whatever `_meta` says.
    build, path = _DATABAG_ROOTS[root]
    serialized = serialize(build(_deep_databag(levels)))
    assert _CANARY in json_dumps(serialized).decode()
    cleaned = scrub_sentry_event(_with_meta(serialized, meta))
    assert isinstance(cleaned, dict)
    _assert_no_canary(json_dumps(cleaned).decode(), repr(cleaned))
    assert _node_at(cleaned, (*path, *_TRIMMED_LEVEL)) == _FILTERED
    assert isinstance(_node_at(cleaned, (*path, *_TRIMMED_LEVEL[:-1])), dict)


@pytest.mark.parametrize("meta", ["sdk", "missing", "empty", "no_limit_mark"])
def test_sdk_depth_repr_inside_lists_never_spells_a_deeper_key(meta):
    # Lists count toward the SDK's depth limit as mappings do.
    attempts = [[[[{f"{_CANARY}\tleaf": 1, _CANARY: 2}]]]]
    serialized = serialize({"extra": {"attempts": attempts}})
    assert _CANARY in json_dumps(serialized).decode()
    cleaned = scrub_sentry_event(_with_meta(serialized, meta))
    assert isinstance(cleaned, dict)
    _assert_no_canary(json_dumps(cleaned).decode(), repr(cleaned))
    assert cleaned["extra"] == {"attempts": [[[[_FILTERED]]]]}


def _safe_chain(levels):
    chain = "disk unavailable"
    for level in reversed(range(levels)):
        chain = {f"level{level}": chain}
    return chain


def _placed(path, value):
    for segment in reversed(path):
        value = [value] if isinstance(segment, int) else {segment: value}
    return value


# Drift pins: where the SDK writes a `!limit` repr for a deep chain. The
# position rule must find every marked repr and leave every other position.
_SDK_TRIM_POSITIONS = {
    ("extra",): True, ("request", "data"): True,
    ("breadcrumbs", "values", 0): True, ("breadcrumbs", "values"): True,
    ("request",): False, ("request", "headers"): False, ("contexts", "sync"): False,
    ("tags",): False, ("user",): False, ("modules",): False,
    ("logentry", "params"): False, ("threads", "values", 0): False,
    ("exception", "values", 0, "mechanism", "data"): False, ("spans", 0, "data"): False,
}


@pytest.mark.parametrize(
    "position", list(_SDK_TRIM_POSITIONS), ids=lambda path: ".".join(map(str, path)))
def test_position_rule_matches_where_the_sdk_trims(position):
    serialized = serialize(_placed(position, _safe_chain(8)))
    marked = '"!limit"' in json.dumps(serialized.get("_meta", {}))
    assert marked is _SDK_TRIM_POSITIONS[position]
    stripped = {key: item for key, item in serialized.items() if key != "_meta"}
    with_meta = scrub_sentry_event(serialized)
    with_meta.pop("_meta", None)
    assert scrub_sentry_event(stripped) == with_meta
    if not marked:
        assert scrub_sentry_event(stripped) == stripped


@pytest.mark.parametrize("root", ["exception_vars", "thread_vars", "stacktrace_vars"])
def test_position_rule_matches_the_early_vars_trim(root):
    build, path = _DATABAG_ROOTS[root]
    serialized = serialize(build(_safe_chain(8)))
    assert "_meta" not in serialized
    assert isinstance(_node_at(serialized, (*path, *_TRIMMED_LEVEL)), str)
    assert isinstance(_node_at(serialized, (*path, *_TRIMMED_LEVEL[:-1])), dict)
    assert _node_at(scrub_sentry_event(serialized), (*path, *_TRIMMED_LEVEL)) == _FILTERED


def test_depth_mark_outside_the_known_roots_is_still_filtered():
    # A later SDK may trim another databag; its `!limit` mark still applies.
    limit = {"": {"rem": [["!limit", "x"]]}}
    event = {"spans": [{"op": "db", "data": {"rows": f"{{'{_CANARY}': 1}}"}}],
             "_meta": {"spans": {"0": {"data": {"rows": limit}}}}}
    cleaned = scrub_sentry_event(event)
    assert cleaned["spans"] == [{"op": "db", "data": {"rows": _FILTERED}}]
    assert cleaned["_meta"] == event["_meta"]


# Names the SDK and the app write. Values below each one are left alone or
# filtered by the existing value rules exactly as before R54-L03.
_STATIC_NAMES_KEEPING_VALUE = (
    "level", "logger", "platform", "release", "environment", "event_id",
    "timestamp", "transaction", "fingerprint", "message", "logentry", "formatted",
    "params", "exception", "values", "type", "module", "mechanism", "handled",
    "meta", "errno", "signal", "number", "name", "stacktrace", "frames",
    "filename", "abs_path", "function", "lineno", "colno", "pre_context",
    "context_line", "post_context", "in_app", "vars", "threads", "current",
    "crashed", "main", "contexts", "os", "build", "kernel_version", "version",
    "raw_description", "runtime", "trace", "trace_id", "span_id",
    "parent_span_id", "op", "description", "status", "origin", "app",
    "app_start_time", "breadcrumbs", "category", "data", "url", "http.method",
    "reason", "tags", "extra", "sys.argv", "modules", "sdk", "packages",
    "integrations", "request", "method", "headers", "Accept", "Content-Type",
    "User-Agent", "X-Request-Id", "debug_meta", "images", "_meta", "len",
    "rem", "revocation_storage.operation", "revocation_storage.cause",
    "revocation_storage", "operation", "cause", "shiboken6", "sentry-sdk",
    "platformdirs", "typing_extensions",
    "zope.interface", "backports.tarfile", "pywin32-ctypes", "x86_64", "arm64",
    "sha256", "python3.12", "utf-8", "ipv6", "http2", "h11",
    "phase", "attempt", "component", "detail", "_private", "__class__",
    "UPPER_CASE", "camelCaseName", "a", "x" * 64,
    "abcdefghijklmnopq1", "v12345", "a1b2c3d4e5",
)
_STATIC_NAMES_FILTERING_VALUE = (
    "token", "password", "authorization", "cookie", "dsn", "session_id",
    "device", "room", "state", "code", "status_code", "key", "value",
    "privateKey", "X-Amz-Signature", "cloud_credential.operation",
    "cloud_credential.cause", "cloud_credential", "server_name_secret",
    # Existing substring rules: `sid` in PySide6 and `auth` in oauthlib.
    "PySide6", "PySide6-Essentials", "oauthlib",
)


@pytest.mark.parametrize("name", _STATIC_NAMES_KEEPING_VALUE)
def test_static_names_and_values_survive_exactly(name):
    for wrap in (lambda body: {"extra": body}, lambda body: {"tags": body},
                 lambda body: {"contexts": {"sync": body}}):
        event = wrap({name: "disk unavailable"})
        assert scrub_sentry_event(event) == event
        assert _final_event(event)[0] == event
    crumb = {"category": "sync", "data": {name: "disk unavailable"}}
    assert scrub_sentry_breadcrumb(crumb) == crumb


@pytest.mark.parametrize("name", _STATIC_NAMES_FILTERING_VALUE)
def test_static_secret_names_survive_with_filtered_values(name):
    expected = {"extra": {name: _FILTERED}}
    assert scrub_sentry_event({"extra": {name: "disk unavailable"}}) == expected
    crumb = {"category": "sync", "data": {name: "disk unavailable"}}
    assert scrub_sentry_breadcrumb(crumb) == {"category": "sync", "data": {name: _FILTERED}}


@pytest.mark.parametrize(("name", "kept"), [
    ("a" * 64, True), ("a" * 65, False),
    ("v12345", True), ("v123456", False), ("v1_2_3_4_5_6", False),
    ("abcdefghijklmn12", False), ("abcdefghijklm12", True),
    ("abcdefghijklmno1", True), ("abc-defghijklmn12", True),
    ("a_b", True), ("_a", True), ("A", True), ("", True), ("1a", False),
    ("0", True), ("12345", True), ("123456", False), ("1.2", False), ("-1", False),
    ("pomodorough1", False), ("Pomodorough1x", False), ("pomodorough2", True),
    ("v1.2.3.4", False), ("v1.2.3", True), ("a@b", False), ("a%b", False),
    ("a~b", False), ("a+b", False), ("a/b", False), ("a:b", False),
])
def test_diagnostic_key_boundaries(name, kept):
    expected = {name: "ok"} if kept else {"[Filtered key 1]": _FILTERED}
    assert scrub_sentry_event({"extra": {name: "ok"}}) == {"extra": expected}


@pytest.mark.parametrize(("name", "kept"), [
    ("123456", False), ("0123456789", False), ("12a", False), ("a12", True),
    ("1.2", False), ("1-2", False), ("1_2", False), ("12 3", False), ("+12", False),
    ("", True), ("0", True), ("404", True),
    # Limits: a short all-digit key, such as a PIN used as a key, is kept.
    ("12345", True),
])
def test_digit_key_policy_on_the_final_event(name, kept):
    data = {name: "disk unavailable", "phase": "pull"}
    crumb = {"category": "sync", "data": dict(data)}
    hooked, final_crumb, crumb_rendered = _final_breadcrumb(crumb)
    cleaned, rendered = _final_event({"extra": dict(data), "tags": dict(data)})
    expected = data if kept else {"[Filtered key 1]": _FILTERED, "phase": "pull"}
    assert cleaned == {"extra": expected, "tags": expected}
    assert hooked["data"] == expected
    assert final_crumb == hooked
    if not kept:
        assert json.dumps(name) not in rendered + crumb_rendered


def test_exact_static_event_control():
    event = {
        "level": "error", "logger": "pomodorough.sync", "platform": "python",
        "environment": "production", "release": "0.54.0",
        "tags": {"revocation_storage.operation": "restore", "component": "sync"},
        "contexts": {
            "os": {"name": "macOS", "version": "15.1", "build": "24B83"},
            "runtime": {"name": "CPython", "version": "3.12.7"},
            "revocation_storage": {"operation": "restore", "cause": "io"},
        },
        "extra": {"sys.argv": ["pomodorough"], "attempt": 2, "ratio": 0.5,
                  "phase": None, "retry": True},
        "exception": {"values": [{
            "type": "OSError", "module": "builtins",
            "mechanism": {"type": "excepthook", "handled": False},
            "stacktrace": {"frames": [{"filename": "pomodorough/sync.py",
                                       "function": "pull", "lineno": 42,
                                       "in_app": True}]},
        }]},
        "breadcrumbs": {"values": [{
            "type": "http", "category": "httplib", "level": "info",
            "data": {"url": "https://example.invalid/sync", "http.method": "GET",
                     "http.query": "page=2", "reason": "Service Unavailable"},
        }]},
        "modules": {"shiboken6": "6.8.0", "sentry-sdk": "2.68.1"},
        "sdk": {"name": "sentry.python", "version": "2.68.1",
                "integrations": ["excepthook", "logging"]},
    }
    assert scrub_sentry_event(event) == event
    assert _final_event(event)[0] == event
    crumb = event["breadcrumbs"]["values"][0]
    assert scrub_sentry_breadcrumb(crumb) == crumb


def test_exact_mixed_control_keeps_neighbors_and_order():
    data = {"phase": "pull", f"{_CANARY}@example.test": "x", "attempt": 2,
            f"{_CANARY}\tb": {"token": "y"}, "detail": "disk unavailable"}
    expected = {"phase": "pull", "[Filtered key 1]": _FILTERED, "attempt": 2,
                "[Filtered key 2]": _FILTERED, "detail": "disk unavailable"}
    cleaned = scrub_sentry_breadcrumb({"category": "sync", "data": data})["data"]
    assert cleaned == expected
    assert list(cleaned) == list(expected)
