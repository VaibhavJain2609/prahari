"""internal_auth.py: the one place the header name, the empty-expected
semantics and the credential→identity resolution live. If two services ever
disagreed on any of them, the gate would be open or closed inconsistently
and nothing would raise.
"""

from __future__ import annotations

import pytest

from prahari_common.internal_auth import (
    HEADER_NAME,
    caller_accepted,
    expected_token_ok,
    gate_posture,
    parse_caller_tokens,
    provided_token,
    resolve_caller,
)


class TestProvidedToken:
    def test_extracts_the_header(self) -> None:
        assert provided_token({HEADER_NAME: "s3cret"}) == "s3cret"

    def test_absent_header_is_none_not_empty(self) -> None:
        # None and "" must stay distinguishable: a gate distinguishes "no
        # credential presented" from "empty credential presented" only if the
        # extractor does.
        assert provided_token({}) is None

    def test_works_on_grpc_style_metadata_dicts(self) -> None:
        # gRPC hands the interceptor a tuple of (key, value) pairs; the caller
        # dict()s it. Keys arrive already lowercased by grpc itself.
        metadata = (("x-internal-token", "tok"), ("user-agent", "grpc-python"))
        assert provided_token(dict(metadata)) == "tok"


class TestExpectedTokenOk:
    def test_empty_expected_disables_the_gate(self) -> None:
        # The local/dev default and the chart's contract: unset means OFF.
        assert expected_token_ok(None, "")
        assert expected_token_ok("", "")
        assert expected_token_ok("anything", "")

    def test_armed_gate_accepts_only_the_exact_token(self) -> None:
        assert expected_token_ok("s3cret", "s3cret")

    def test_armed_gate_rejects_wrong_missing_and_empty(self) -> None:
        assert not expected_token_ok("wrong", "s3cret")
        assert not expected_token_ok(None, "s3cret")
        assert not expected_token_ok("", "s3cret")

    def test_prefix_is_not_a_match(self) -> None:
        # compare_digest, not startswith: a truncated token must not pass.
        assert not expected_token_ok("s3cr", "s3cret")


class TestResolveCaller:
    """The credential→identity half of the isolated gate: a presented token
    maps to a caller name, and anything unmapped is anonymous."""

    TOKENS = {"bff": "tok-bff", "correlation": "tok-cor", "inference": "tok-inf"}

    def test_each_token_resolves_to_its_own_caller(self) -> None:
        assert resolve_caller("tok-bff", self.TOKENS) == "bff"
        assert resolve_caller("tok-cor", self.TOKENS) == "correlation"
        assert resolve_caller("tok-inf", self.TOKENS) == "inference"

    def test_unknown_token_resolves_to_none(self) -> None:
        assert resolve_caller("nope", self.TOKENS) is None

    def test_absent_and_empty_tokens_resolve_to_none(self) -> None:
        # Absent and empty stay indistinguishable-but-both-anonymous, same
        # rule expected_token_ok keeps.
        assert resolve_caller(None, self.TOKENS) is None
        assert resolve_caller("", self.TOKENS) is None

    def test_empty_map_resolves_nothing(self) -> None:
        assert resolve_caller("tok-bff", {}) is None

    def test_empty_map_entries_never_match(self) -> None:
        # A tokenless caller entry must not make the empty presented token
        # resolve — "" matching "" would arm an anonymous identity.
        assert resolve_caller("", {"bff": ""}) is None

    def test_a_token_matching_two_names_resolves_to_one_of_them(self) -> None:
        # Duplicate secrets are a misconfiguration, not a security boundary:
        # the resolver must still return a single deterministic name rather
        # than a set (and never crash on the overlap).
        assert resolve_caller("same", {"a": "same", "b": "same"}) in {"a", "b"}


class TestCallerAccepted:
    """The three postures, pinned: open (nothing configured), shared (only
    internal_token), isolated (a caller map)."""

    def test_open_when_nothing_is_configured(self) -> None:
        assert caller_accepted(None, internal_token="", caller_tokens={}, accepted_callers=set())

    def test_shared_mode_is_the_legacy_single_token_check(self) -> None:
        assert caller_accepted(
            "tok", internal_token="tok", caller_tokens={}, accepted_callers=set()
        )
        assert not caller_accepted(
            "wrong", internal_token="tok", caller_tokens={}, accepted_callers=set()
        )
        assert not caller_accepted(
            None, internal_token="tok", caller_tokens={}, accepted_callers=set()
        )

    def test_shared_mode_ignores_the_allowlist(self) -> None:
        # The allowlist only exists in isolated mode — pinning this keeps a
        # shared-mode deployment from silently requiring map plumbing.
        assert caller_accepted(
            "tok",
            internal_token="tok",
            caller_tokens={},
            accepted_callers=set(),
        )

    def test_isolated_mode_accepts_an_allowlisted_caller(self) -> None:
        assert caller_accepted(
            "tok-bff",
            internal_token="shared",
            caller_tokens={"bff": "tok-bff", "inference": "tok-inf"},
            accepted_callers={"bff"},
        )

    def test_isolated_mode_denies_a_known_caller_not_on_the_allowlist(self) -> None:
        # The token is VALID — it just belongs to a caller this service does
        # not accept. This is the whole point of per-service credentials: a
        # leaked inference token must not open a bff-only surface.
        assert not caller_accepted(
            "tok-inf",
            internal_token="shared",
            caller_tokens={"bff": "tok-bff", "inference": "tok-inf"},
            accepted_callers={"bff"},
        )

    def test_isolated_mode_denies_an_unknown_token(self) -> None:
        assert not caller_accepted(
            "nope",
            internal_token="shared",
            caller_tokens={"bff": "tok-bff"},
            accepted_callers={"bff"},
        )
        assert not caller_accepted(
            None,
            internal_token="shared",
            caller_tokens={"bff": "tok-bff"},
            accepted_callers={"bff"},
        )

    def test_isolated_mode_accepts_the_internal_compat_caller(self) -> None:
        # internal_token resolves to "internal", accepted everywhere — the
        # migration path for callers still holding the shared credential.
        assert caller_accepted(
            "shared",
            internal_token="shared",
            caller_tokens={"bff": "tok-bff"},
            accepted_callers={"bff"},
        )

    def test_isolated_mode_without_internal_token_has_no_compat_caller(self) -> None:
        # caller_tokens configured but internal_token empty: the shared
        # credential does not exist in this deployment, so nothing may
        # resolve to "internal".
        assert not caller_accepted(
            "shared",
            internal_token="",
            caller_tokens={"bff": "tok-bff"},
            accepted_callers={"bff"},
        )
        assert caller_accepted(
            "tok-bff",
            internal_token="",
            caller_tokens={"bff": "tok-bff"},
            accepted_callers={"bff"},
        )

    def test_isolated_mode_still_denies_wrong_token_when_only_map_set(self) -> None:
        # A map alone (no shared token) arms the gate — it does not open it.
        assert not caller_accepted(
            "wrong",
            internal_token="",
            caller_tokens={"bff": "tok-bff"},
            accepted_callers={"bff"},
        )


class TestGatePosture:
    def test_posture_labels(self) -> None:
        assert gate_posture("", {}) == "open"
        assert gate_posture("tok", {}) == "shared"
        assert gate_posture("tok", {"bff": "t"}) == "isolated"
        # A configured map is the stronger posture even with no shared token.
        assert gate_posture("", {"bff": "t"}) == "isolated"


class TestParseCallerTokens:
    """The settings-side parser: JSON object, comma form, and the inputs that
    must produce the empty map (which reads as "not configured")."""

    def test_json_object(self) -> None:
        assert parse_caller_tokens('{"bff": "t1", "inference": "t2"}') == {
            "bff": "t1",
            "inference": "t2",
        }

    def test_comma_form(self) -> None:
        assert parse_caller_tokens("bff:t1,inference:t2") == {
            "bff": "t1",
            "inference": "t2",
        }

    def test_comma_form_tolerates_whitespace(self) -> None:
        assert parse_caller_tokens(" bff : t1 , inference : t2 ") == {
            "bff": "t1",
            "inference": "t2",
        }

    def test_mapping_passthrough(self) -> None:
        assert parse_caller_tokens({"bff": "t1"}) == {"bff": "t1"}

    def test_empty_inputs_mean_not_configured(self) -> None:
        assert parse_caller_tokens(None) == {}
        assert parse_caller_tokens("") == {}
        assert parse_caller_tokens("   ") == {}
        assert parse_caller_tokens({}) == {}

    def test_entries_with_empty_names_or_tokens_are_dropped(self) -> None:
        assert parse_caller_tokens("bff:,correlation:t2,:t3") == {"correlation": "t2"}

    def test_non_object_json_raises(self) -> None:
        with pytest.raises(ValueError, match="object"):
            parse_caller_tokens('["bff"]')
