"""RedisStreamConsumer tested against an injected fake client, never a real or
fake Redis server -- matches how `prahari-match`'s `RedisStreamPublisher` /
`RedisDetectionPublisher` are tested: the redis-py surface used here is one
method (`xread`), so a hand-written fake is simpler and more honest than
adding a `fakeredis` dependency this repo does not otherwise need.
"""

from __future__ import annotations

from prahari_common.bus import RedisStreamConsumer


class _FakeRedis:
    """Scripts a sequence of `xread` responses, one per call, in redis-py's
    own shape: `[(stream_name, [(entry_id, {field: value}), ...])]`."""

    def __init__(self, responses: list[list]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict] = []

    def xread(self, streams, count=None, block=None):  # noqa: ANN001
        self.calls.append(dict(streams))
        return next(self._responses, [])


def _consumer(client: _FakeRedis, **kwargs) -> RedisStreamConsumer[str]:
    return RedisStreamConsumer(
        redis_url="redis://unused",
        stream_key="prahari:test",
        field="payload",
        decode=lambda raw: raw.decode(),
        client=client,
        **kwargs,
    )


def test_decodes_entries_and_returns_them_in_order() -> None:
    client = _FakeRedis(
        [
            [
                (
                    b"prahari:test",
                    [
                        (b"1-0", {b"payload": b"first"}),
                        (b"2-0", {b"payload": b"second"}),
                    ],
                )
            ]
        ]
    )
    consumer = _consumer(client)

    assert consumer.poll() == ["first", "second"]


def test_advances_offset_and_passes_it_to_the_next_xread() -> None:
    client = _FakeRedis(
        [
            [(b"prahari:test", [(b"5-0", {b"payload": b"a"})])],
            [(b"prahari:test", [(b"6-0", {b"payload": b"b"})])],
        ]
    )
    consumer = _consumer(client)

    consumer.poll()
    consumer.poll()

    assert client.calls[0] == {"prahari:test": "$"}  # default start_id
    assert client.calls[1] == {"prahari:test": b"5-0"}  # advanced past the first entry


def test_start_id_is_configurable_for_replay() -> None:
    client = _FakeRedis([[]])
    consumer = _consumer(client, start_id="0")

    consumer.poll()

    assert client.calls[0] == {"prahari:test": "0"}


def test_entry_missing_the_expected_field_is_skipped_not_raised() -> None:
    client = _FakeRedis(
        [
            [
                (
                    b"prahari:test",
                    [
                        (b"1-0", {b"other_field": b"x"}),
                        (b"2-0", {b"payload": b"ok"}),
                    ],
                )
            ]
        ]
    )
    consumer = _consumer(client)

    assert consumer.poll() == ["ok"]


def test_one_entry_that_fails_to_decode_does_not_drop_the_rest() -> None:
    def flaky_decode(raw: bytes) -> str:
        if raw == b"bad":
            raise ValueError("simulated decode failure")
        return raw.decode()

    client = _FakeRedis(
        [
            [
                (
                    b"prahari:test",
                    [
                        (b"1-0", {b"payload": b"bad"}),
                        (b"2-0", {b"payload": b"good"}),
                    ],
                )
            ]
        ]
    )
    consumer = RedisStreamConsumer(
        redis_url="redis://unused",
        stream_key="prahari:test",
        field="payload",
        decode=flaky_decode,
        client=client,
    )

    assert consumer.poll() == ["good"]


def test_a_poisoned_entry_still_advances_the_offset() -> None:
    # Otherwise a message that can never decode wedges the consumer on it
    # forever -- every subsequent poll would re-read the same bad entry.
    def always_fails(raw: bytes) -> str:
        raise ValueError("simulated decode failure")

    client = _FakeRedis(
        [
            [(b"prahari:test", [(b"1-0", {b"payload": b"bad"})])],
            [],
        ]
    )
    consumer = RedisStreamConsumer(
        redis_url="redis://unused",
        stream_key="prahari:test",
        field="payload",
        decode=always_fails,
        client=client,
    )

    consumer.poll()
    consumer.poll()

    assert client.calls[1] == {"prahari:test": b"1-0"}


def test_connection_failure_logs_and_returns_empty_list_without_raising() -> None:
    class _BrokenClient:
        def xread(self, streams, count=None, block=None):  # noqa: ANN001
            raise ConnectionError("simulated redis outage")

    consumer = _consumer(_BrokenClient())  # type: ignore[arg-type]

    assert consumer.poll() == []
