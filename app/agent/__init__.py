"""A's agent layer: the LLM zone.

Everything in this package is allowed to be wrong and retried. Nothing here may
approve, reserve, sign or pay -- those live in ``app.commerce`` and are owned by
C.

The package starts with the two pieces needed to answer one question before the
orchestrator is built: *when the user speaks, does a model actually return the
shape the contracts demand?*
"""

from __future__ import annotations

__all__: list[str] = []
