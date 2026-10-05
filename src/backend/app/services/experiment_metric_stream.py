"""Incremental line framing for bounded experiment stdout/stderr chunks."""

from __future__ import annotations


class MetricLineFramer:
    """Keep independent stream tails until a complete protocol line is available.

    A metric may span several transport reads. Combining unrelated stdout and
    stderr tails would invent a line, so each stream carries its own buffer.
    Oversized unterminated lines are discarded until their newline to bound RAM.
    """

    def __init__(self, *, max_pending_chars: int = 1_048_576) -> None:
        if max_pending_chars < 1:
            raise ValueError("max_pending_chars must be positive")
        self._max_pending_chars = max_pending_chars
        self._pending: dict[str, str] = {}
        self._discarding: set[str] = set()

    def feed(self, stream: str, text: str) -> str:
        """Return complete newline-terminated lines; retain the trailing fragment."""
        if stream in self._discarding:
            _, separator, text = text.partition("\n")
            if not separator:
                return ""
            self._discarding.remove(stream)
        combined = self._pending.pop(stream, "") + text
        last_newline = combined.rfind("\n")
        if last_newline < 0:
            complete, pending = "", combined
        else:
            complete, pending = combined[: last_newline + 1], combined[last_newline + 1 :]
        if len(pending) > self._max_pending_chars:
            self._discarding.add(stream)
        elif pending:
            self._pending[stream] = pending
        return complete

    def finish(self, stream: str | None = None) -> str:
        """Flush terminal non-newline tails once, keeping stream boundaries intact."""
        streams = [stream] if stream is not None else list(self._pending)
        tails: list[str] = []
        for name in streams:
            tail = self._pending.pop(name, "")
            self._discarding.discard(name)
            if tail:
                tails.append(tail)
        if stream is None:
            self._discarding.clear()
        return "\n".join(tails)
