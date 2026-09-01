"""Canonical runtime components for BZU Signal Bot v9.5.59.

The package deliberately contains only release-independent orchestration,
forward analytics, and setup-family identity.  Market detectors remain in the
legacy-compatible application module until they can be migrated one family at
a time without changing live behaviour.
"""

from .execution_engine import CanonicalExecutionEngine, ExecutionEngineHooks
from .forward_control import build_forward_control_snapshot
from .setup_families import (
    CANONICAL_SETUP_FAMILY_MAP,
    canonical_setup_family,
    deduplicate_candidates_by_family_episode,
    family_episode_key,
)

__all__ = [
    "CANONICAL_SETUP_FAMILY_MAP",
    "CanonicalExecutionEngine",
    "ExecutionEngineHooks",
    "build_forward_control_snapshot",
    "canonical_setup_family",
    "deduplicate_candidates_by_family_episode",
    "family_episode_key",
]
