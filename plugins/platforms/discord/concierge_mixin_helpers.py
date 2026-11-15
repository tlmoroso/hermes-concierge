"""Leaf helpers for the concierge Discord mixin and its topical siblings.

Pure helpers only. This module must NOT import ``concierge_mixin`` (the facade):
the facade imports this module, and the topical siblings import both, so a
facade import here would be a module-level cycle. Keeping these small functions
here also takes behaviour OUT of the facade (each helper keeps its own CC cap).
"""
from __future__ import annotations


def _concierge_threading_mode_of(th):
    """``threading.mode`` from a config dict or an attribute-bearing object."""
    return th.get("mode") if isinstance(th, dict) else getattr(th, "mode", None)


def _concierge_dispatch_reply_block(title, worker_answer):
    """Labeled context block the orchestrator sees in the phase-B turn."""
    if worker_answer is not None:
        return f"[{title} replied]\n{worker_answer}"
    return f"[{title} is unavailable]\n(the dispatch could not reach {title})"
