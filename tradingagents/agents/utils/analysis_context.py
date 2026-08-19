"""Immutable analysis-cutoff helpers shared by GX tools and analysts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def effective_tool_cutoff(
    state: Mapping[str, Any] | None,
    fallback: str | None,
) -> str | None:
    """Return the frozen live cutoff, never a model-supplied replacement.

    Close/date-only and upstream graph calls intentionally retain the public
    tool argument for backward compatibility.  ``StageRunner`` injects the
    immutable session values into graph state for live GX runs.
    """
    if state and str(state.get("analysis_mode", "close")).lower() == "live":
        cutoff = state.get("analysis_cutoff")
        if not isinstance(cutoff, str) or not cutoff.strip():
            raise ValueError("live analysis state is missing analysis_cutoff")
        return cutoff.strip()
    return fallback


def analysis_prompt_cutoff(state: Mapping[str, Any]) -> tuple[str, str]:
    """Return prompt cutoff and a concise immutable-PIT instruction."""
    mode = str(state.get("analysis_mode", "close")).lower()
    cutoff = effective_tool_cutoff(state, str(state["trade_date"]))
    if mode == "live":
        return (
            str(cutoff),
            "This is a live point-in-time run. Do not use or infer evidence "
            f"published after the frozen cutoff {cutoff}.",
        )
    return str(cutoff), "This is a completed-market-close analysis."


__all__ = ["analysis_prompt_cutoff", "effective_tool_cutoff"]
