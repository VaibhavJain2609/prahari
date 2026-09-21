"""`__main__.py`: the container entrypoint must invoke `worker.main` — nothing
more. Exercised via runpy with `worker.main` patched out, so the test asserts
the wiring without starting a worker."""

from __future__ import annotations

import runpy

import prahari_inference.worker as worker_module


def test_dunder_main_invokes_worker_main(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(worker_module, "main", lambda: calls.append("main"))

    runpy.run_module("prahari_inference.__main__", run_name="__main__")

    assert calls == ["main"]
