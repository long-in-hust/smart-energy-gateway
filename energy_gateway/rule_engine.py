"""
Rule Engine for Smart Energy Management Gateway
Evaluates energy management rules against current system state.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("rule-engine")


@dataclass
class RuleAction:
    load_id: str
    action: str   # "on" | "off"
    reason: str


@dataclass
class RuleEvent:
    event_type: str
    severity: str   # "info" | "warning" | "critical"
    value: float
    threshold: float
    message: str
    action_taken: Optional[str] = None
    load_id: Optional[str] = None


@dataclass
class RuleResult:
    actions: list[RuleAction] = field(default_factory=list)
    events:  list[RuleEvent]  = field(default_factory=list)


# Priority order for overload shedding (lowest priority shed first)
PRIORITY_ORDER = {"low": 0, "medium": 1, "high": 2}


class RuleEngine:
    def __init__(self, overload_threshold: float = 3500.0):
        self.overload_threshold = overload_threshold
        logger.info("RuleEngine initialised | overload_threshold=%.1fW", overload_threshold)

    def update_threshold(self, new_threshold: float) -> None:
        """Allow API to change threshold at runtime."""
        old = self.overload_threshold
        self.overload_threshold = new_threshold
        logger.info("Overload threshold updated: %.1f -> %.1f W", old, new_threshold)

    def evaluate(self, meter_states: dict, solar_power: float) -> RuleResult:
        """
        Evaluate all rules and return actions + events.

        meter_states: {load_id: {"power_watt": float, "switch": str, "priority": str, ...}}
        solar_power:  current solar generation in watts
        """
        result = RuleResult()

        # ── Rule 1: Calculate total power ───────────────────────────────────
        total_power = sum(
            s.get("power_watt", 0.0)
            for s in meter_states.values()
            if s.get("switch", "on") == "on"
        )
        grid_power = max(0.0, total_power - solar_power)

        logger.debug("total=%.1fW  solar=%.1fW  grid=%.1fW  threshold=%.1fW",
                     total_power, solar_power, grid_power, self.overload_threshold)

        # ── Rule 2: Overload detection ───────────────────────────────────────
        if total_power > self.overload_threshold:
            # create a rule event
            event = RuleEvent(
                event_type="overload_detected",
                severity="warning",
                value=round(total_power, 2),
                threshold=self.overload_threshold,
                message=f"Total power {total_power:.1f}W exceeds threshold {self.overload_threshold:.1f}W",
            )

            # ── Rule 3: Overload shedding — shed lowest priority first ───────
            # Sort active loads by priority ascending (low first)
            active_loads = [
                (lid, s) for lid, s in meter_states.items()
                if s.get("switch", "on") == "on"
            ]
            active_loads.sort(key=lambda x: PRIORITY_ORDER.get(x[1].get("priority", "low"), 0))

            shed_power = 0.0
            shed_actions = []
            for load_id, s in active_loads:
                # Shed enough loads to bring total_power below threshold
                if total_power - shed_power <= self.overload_threshold:
                    break
                if s.get("priority", "low") == "high":
                    continue  # never shed high priority unless no choice
                shed_power += s.get("power_watt", 0.0)
                shed_actions.append(
                    RuleAction(load_id=load_id, action="off", reason="overload_shedding")
                )
                logger.info("Shedding load '%s' (%.1fW, priority=%s)",
                            load_id, s.get("power_watt", 0), s.get("priority"))

            if shed_actions:
                event.action_taken = f"shed: {', '.join(a.load_id for a in shed_actions)}"
                result.actions.extend(shed_actions)

            result.events.append(event)

        # ── Rule 4: Restore loads when solar is high ─────────────────────────
        if solar_power > 500.0:
            for load_id, s in meter_states.items():
                if s.get("switch", "on") == "off" and s.get("last_reason") == "overload_shedding":
                    projected = total_power + s.get("power_watt", 0.0)
                    if projected < self.overload_threshold * 0.85:  # 15% headroom
                        result.actions.append(
                            RuleAction(load_id=load_id, action="on", reason="solar_surplus_restore")
                        )
                        result.events.append(RuleEvent(
                            event_type="load_restored",
                            severity="info",
                            value=round(solar_power, 2),
                            threshold=500.0,
                            message=f"Restoring {load_id} — solar surplus available",
                            action_taken=f"restore_{load_id}",
                            load_id=load_id,
                        ))
                        logger.info("Restoring load '%s' due to solar surplus (%.1fW)", load_id, solar_power)
                        total_power += s.get("power_watt", 0.0)  # update running total

        # ── Rule 5: Meter offline detection is handled in gateway.py ─────────

        return result, round(total_power, 2), round(grid_power, 2)