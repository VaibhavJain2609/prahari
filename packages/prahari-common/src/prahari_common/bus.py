"""A Redis Stream of protobuf messages, tracking an offset -- the plumbing
`services/correlation` (reading `prahari:detections`) and `services/bff`
(relaying `prahari:alerts` over SSE) both need identically. Shared here rather
than duplicated per DAY3-DESIGN.md §3.5, the same reasoning as `plates.py`:
two consumers must agree on how a stream entry is read, or one silently
drifts.

`redis` is imported lazily, inside `_client_or_connect`, matching
`RedisDetectionPublisher`/`RedisStreamPublisher` in `prahari-match` --
importing this module must not require `redis` to be installed, and
`prahari-common` deliberately does not list it as a hard dependency; a
consumer service (correlation, bff) declares it in its own `pyproject.toml`,
the same way `prahari-match` does today.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

__all__ = ["RedisStreamConsumer"]

log = logging.getLogger(__name__)


class _RedisLike(Protocol):
    """The one redis-py method this class calls -- narrow on purpose, so a
    test double only has to implement `xread`, not the whole client."""

    def xread(
        self, streams: dict[str, str], count: int | None = None, block: int | None = None
    ) -> list: ...


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


class RedisStreamConsumer[M]:
    """Polls one Redis Stream and decodes each entry's `field` with `decode`.

    Deliberately not an iterator/generator: `poll()` returns a bounded batch
    for the caller's own loop to drive, so a caller that also needs to serve
    HTTP requests (bff's SSE relay) or run on a scheduler tick controls its
    own blocking, rather than this class owning the loop.

    `start_id="$"` (the default) means "only entries appended after this
    consumer connects" -- the right default for both known callers: an SSE
    relay must not replay history to every new browser tab, and correlation
    is meant to run continuously, not replay the whole detections stream on
    every restart. A caller that needs replay-from-start (e.g. rebuilding
    state after a restart) passes `start_id="0"` explicitly.
    """

    def __init__(
        self,
        redis_url: str,
        stream_key: str,
        field: str,
        decode: Callable[[bytes], M],
        *,
        start_id: str = "$",
        block_ms: int = 5000,
        count: int = 100,
        client: _RedisLike | None = None,
    ) -> None:
        self._redis_url = redis_url
        self._stream_key = stream_key
        self._field = field
        self._decode = decode
        self._last_id = start_id
        self._block_ms = block_ms
        self._count = count
        self._client = client

    def _client_or_connect(self) -> _RedisLike:
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(self._redis_url)
        return self._client

    def poll(self) -> list[M]:
        """Blocks up to `block_ms` for new entries, decodes what arrived, and
        advances the offset past every entry seen -- including ones that
        failed to decode, so one poisoned message cannot wedge the consumer
        on it forever. A connection failure logs and returns `[]`: the same
        "log and swallow, never raise" rule the publishers use, since a
        Redis blip must not crash the service polling this."""
        try:
            client = self._client_or_connect()
            response = client.xread(
                {self._stream_key: self._last_id}, count=self._count, block=self._block_ms
            )
        except Exception:
            log.exception("failed to read redis stream %s", self._stream_key)
            return []

        messages: list[M] = []
        for _stream_name, entries in response:
            for entry_id, fields in entries:
                self._last_id = entry_id
                raw = fields.get(self._field) or fields.get(self._field.encode())
                if raw is None:
                    log.warning(
                        "stream %s entry %s has no field %r; skipping",
                        self._stream_key,
                        _text(entry_id),
                        self._field,
                    )
                    continue
                try:
                    messages.append(self._decode(raw))
                except Exception:
                    log.exception(
                        "failed to decode stream %s entry %s; skipping",
                        self._stream_key,
                        _text(entry_id),
                    )
        return messages
