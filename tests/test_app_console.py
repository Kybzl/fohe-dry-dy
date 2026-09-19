from __future__ import annotations

import app


class _Stream:
    def __init__(self) -> None:
        self.options: dict[str, str] = {}

    def reconfigure(self, **kwargs: str) -> None:
        self.options = kwargs


def test_console_is_reconfigured_for_utf8(monkeypatch) -> None:
    stdout = _Stream()
    stderr = _Stream()
    monkeypatch.setattr(app.sys, "stdout", stdout)
    monkeypatch.setattr(app.sys, "stderr", stderr)

    app._configure_console_encoding()

    assert stdout.options == {"encoding": "utf-8", "errors": "replace"}
    assert stderr.options == {"encoding": "utf-8", "errors": "replace"}
