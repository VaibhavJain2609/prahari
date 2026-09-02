"""A Bloom filter over watchlist plate skeletons.

Stage 1 of the matcher (DAY2-DESIGN.md §7.2): at statewide event rates, a
per-detection scan of the watchlist is not viable, so almost-all non-watchlist
traffic must be rejected in O(1) time and bounded memory before any scoring
runs. A Bloom filter is the standard tool for exactly that trade.

Pure Python, no new dependency -- the filter itself is a few hundred lines of
arithmetic over a bit array, not worth a library for a hackathon-scale
watchlist.

INVARIANT: a Bloom filter never produces a false NEGATIVE. If `skeleton not in
bloom`, that skeleton genuinely was never added. It CAN produce a false
POSITIVE (says "maybe" for something never added); `current_false_positive_rate`
makes the size of that one-sided error visible instead of an assumption, since
the entire funnel's correctness rests on stage 1 never wrongly saying no.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable

__all__ = ["BloomFilter"]


class BloomFilter:
    def __init__(self, expected_items: int, target_fp_rate: float = 0.01) -> None:
        if expected_items <= 0:
            raise ValueError("expected_items must be positive")
        if not 0.0 < target_fp_rate < 1.0:
            raise ValueError("target_fp_rate must be in (0, 1)")

        self._expected_items = expected_items
        self._target_fp_rate = target_fp_rate

        # Standard optimal sizing (Broder & Mitzenmacher): m bits and k hashes
        # for n expected items at the target false-positive rate.
        self._size = max(
            8, math.ceil(-(expected_items * math.log(target_fp_rate)) / (math.log(2) ** 2))
        )
        self._hash_count = max(1, round((self._size / expected_items) * math.log(2)))

        self._bits = bytearray((self._size + 7) // 8)
        # Items actually added, not `expected_items`. The observed count is what
        # drives the honest fill rate below; the estimate only sized the array.
        self._count = 0

    def _indices(self, item: str) -> Iterable[int]:
        """Kirsch-Mitzenmacher double hashing: derive all k index functions
        from two independent hashes of the same digest rather than running k
        separate hash functions. Provably as good in practice as k independent
        hashes, and one sha256 call is cheaper than k of them."""
        digest = hashlib.sha256(item.encode("utf-8")).digest()
        h1 = int.from_bytes(digest[:16], "big")
        h2 = int.from_bytes(digest[16:], "big")
        for i in range(self._hash_count):
            yield (h1 + i * h2) % self._size

    def add(self, item: str) -> None:
        for idx in self._indices(item):
            self._bits[idx // 8] |= 1 << (idx % 8)
        self._count += 1

    def __contains__(self, item: str) -> bool:
        return all(self._bits[idx // 8] & (1 << (idx % 8)) for idx in self._indices(item))

    def __len__(self) -> int:
        """Items added, not distinct bits set -- a duplicate `add` is a no-op
        on the bit array but still counts here, since it reflects watchlist
        size, which is what `current_false_positive_rate` needs."""
        return self._count

    @property
    def current_false_positive_rate(self) -> float:
        """The rate implied by what has actually been inserted, not the
        `target_fp_rate` the filter was sized for. Diverges from the target
        once more items land than `expected_items` planned for -- surfaced on
        `/readyz` so a caller can decide to rebuild rather than silently
        absorb a worse rejection rate."""
        if self._count == 0:
            return 0.0
        exponent = -self._hash_count * self._count / self._size
        return (1.0 - math.exp(exponent)) ** self._hash_count

    @property
    def size_bits(self) -> int:
        return self._size

    @property
    def hash_count(self) -> int:
        return self._hash_count
