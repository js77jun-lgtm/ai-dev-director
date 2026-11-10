"""
V3 TRIAD RESEARCH MODE - provider readiness assessment.

Read-only. Reuses core.provider_manager.ProviderManager's existing, already-tested usability computation
(usable_domains()) instead of re-deriving provider health from scratch. Never writes to
provider_health.current_role - that column belongs to NORMAL mode's ProviderManager.resolve_roles() and a
concurrent TRIAD cycle must never fight NORMAL mode over what it says.

TRIAD_READY   - all three of Codex/Antigravity/Claude are usable right now.
TRIAD_DEGRADED - exactly one is unusable. A degraded 2-provider session is never silently treated as a full
                 TRIAD success (PHASE 3): the caller (research session intake, milestone B) must record an
                 explicit `degraded=True` + the missing domain in the session's own evidence, and requires
                 human approval before proceeding at all with two providers.
TRIAD_BLOCKED  - two or three are unusable. A session cannot start.
"""
from typing import Dict, List

from core.provider_manager import ProviderManager

ALL_TRIAD_DOMAINS = ("OPENAI", "ANTHROPIC", "GOOGLE")

TRIAD_READY = "TRIAD_READY"
TRIAD_DEGRADED = "TRIAD_DEGRADED"
TRIAD_BLOCKED = "TRIAD_BLOCKED"


def assess_triad_readiness(pm: ProviderManager) -> Dict[str, object]:
    """Read-only snapshot of whether a TRIAD research cycle can start right now. Never mutates provider
    state beyond whatever ProviderManager.usable_domains() itself already does (nothing - it's a pure
    read of self.status_map plus trial-claim bookkeeping already owned by ProviderManager)."""
    usable: List[str] = sorted(set(pm.usable_domains()) & set(ALL_TRIAD_DOMAINS))
    missing: List[str] = sorted(set(ALL_TRIAD_DOMAINS) - set(usable))

    if not missing:
        state = TRIAD_READY
    elif len(missing) == 1:
        state = TRIAD_DEGRADED
    else:
        state = TRIAD_BLOCKED

    return {
        "state": state,
        "usable": usable,
        "missing": missing,
        "all_three_usable": not missing,
    }
