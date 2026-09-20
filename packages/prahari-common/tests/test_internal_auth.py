"""internal_auth.py: the one place the header name and the empty-expected
semantics live. If two services ever disagreed on either, the gate would be
open or closed inconsistently and nothing would raise.
"""

from __future__ import annotations

from prahari_common.internal_auth import HEADER_NAME, expected_token_ok, provided_token


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
