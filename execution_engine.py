"""One canonical orchestration boundary for trade/no-trade execution.

The engine is dependency-injected so it can operate on the application's
existing Candidate/Decision dataclasses without importing the monolith or
creating a circular dependency.  Historical release functions are callbacks;
only this class is allowed to compose the live v9.5.59 decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


SCHEMA_VERSION = "canonical_execution_engine_v9.5.59"


@dataclass(frozen=True)
class ExecutionEngineHooks:
    base_executor: Callable[[Any, dict[str, Any], dict[str, Any]], Any]
    router_reader: Callable[[Any], dict[str, Any]]
    profile_builder: Callable[..., dict[str, Any]]
    profile_applier: Callable[..., Any]
    directional_guard: Callable[[Any, Any], dict[str, Any]]
    approval: Callable[[Any], Any]
    anchor_recorder: Callable[..., None]
    safe_float: Callable[[Any, float], float]
    executable_actions: frozenset[str]


class CanonicalExecutionEngine:
    """Compose Router evidence, authority profile, and risk exactly once."""

    def __init__(self, hooks: ExecutionEngineHooks) -> None:
        self._hooks = hooks

    def execute(
        self,
        decision: Any,
        context: dict[str, Any],
        journal: dict[str, Any],
    ) -> Any:
        candidate = getattr(decision, "candidate", None)
        if candidate is None:
            return decision

        original_action = str(getattr(decision, "action", "") or "")
        plan = getattr(decision, "plan", None)
        planned_risk = self._hooks.safe_float(
            getattr(plan, "position_risk_pct", 0.0),
            self._hooks.safe_float(getattr(plan, "risk_pct", 0.0), 0.0),
        )
        shadow_context = dict(context)
        shadow_context["_audit_shadow_scan"] = True
        out = self._hooks.base_executor(decision, shadow_context, journal)
        out.audit = getattr(out, "audit", None) or {}

        router = dict(self._hooks.router_reader(out) or {})
        authority = dict(out.audit.get("final_execution_authority_v9551") or {})
        base_profile = dict(
            authority.get("four_level_execution_v9558")
            or authority.get("three_level_execution")
            or {}
        )
        guard = dict(
            out.audit.get("directional_market_guard_v9555")
            or self._hooks.directional_guard(candidate, getattr(out, "plan", None))
            or {}
        )
        profile = self._hooks.profile_builder(
            candidate,
            getattr(out, "plan", None),
            base_profile=base_profile,
            router=router,
            directional_guard=guard,
        )
        out = self._hooks.profile_applier(
            out,
            profile,
            planned_risk=planned_risk,
            context=context,
            journal=journal,
        )
        out.audit = getattr(out, "audit", None) or {}
        trace = {
            "engine": "CanonicalExecutionEngine",
            "router_read_count": 1,
            "profile_build_count": 1,
            "risk_authority_count": 1,
            "final_action_mutator_count": 1,
            "entry_tier": profile.get("tier"),
            "legacy_layers_are_callbacks_only": True,
            "schema_version": SCHEMA_VERSION,
        }
        out.audit["canonical_execution_engine_v9559"] = trace
        candidate.stage_plan = getattr(candidate, "stage_plan", None) or {}
        candidate.stage_plan["canonical_execution_engine_v9559"] = dict(trace)
        candidate.stage_plan["final_execution_tier_v9559"] = profile.get("tier")

        authority = dict(out.audit.get("final_execution_authority_v9551") or {})
        authority.update({
            "canonical_engine": "CanonicalExecutionEngine",
            "entry_tier": profile.get("tier"),
            "single_runtime_mutator": True,
            "schema_version": SCHEMA_VERSION,
        })
        out.audit["final_execution_authority_v9551"] = authority
        director = out.audit.setdefault("executive_director", {})
        report = director.setdefault("report", {})
        executive = report.setdefault("executive_decision", {})
        executive.setdefault("audit", {})["final_execution_authority"] = authority
        report.setdefault("audit", {})["final_execution_authority"] = authority
        director.update({
            "authority": "100% CANONICAL_EXECUTION_ENGINE_V9_5_59",
            "canonical_engine_trace": trace,
        })

        if not context.get("_audit_shadow_scan"):
            self._hooks.anchor_recorder(
                journal,
                candidate,
                authority,
                recovered=(
                    str(getattr(out, "action", "") or "") in self._hooks.executable_actions
                    and not profile.get("anchor_strong")
                ),
                old_entry_available=original_action in self._hooks.executable_actions,
            )
        return self._hooks.approval(out)
