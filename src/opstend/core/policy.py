"""Automation policy: how much OpsTend may do on its own.

Autonomy is set per action category. Budgets and cooldowns stop retry loops.
The kill switch pauses every automatic change while observation continues.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import Field

from opstend.core.models import Model
from opstend.core.windows import Window
from opstend.i18n import tr


class Autonomy(StrEnum):
    OBSERVE = "observe"  # record only
    PROPOSE = "propose"  # prepare a job and wait for approval
    AUTO = "auto"  # run automatically within budgets and windows


class Category(StrEnum):
    RESTART = "restart"  # restart an unhealthy service or guest
    RESCHEDULE = "reschedule"  # move a role or workload off a failed node
    PATCH = "patch"  # security updates of OS packages
    UPGRADE = "upgrade"  # feature updates (OpsTend, Proxmox, firmware, apps)
    QUARANTINE = "quarantine"  # withdraw exposure of a critically vulnerable service
    NETWORK = "network"  # changes on switches, VLANs, firewall
    DEPLOY = "deploy"  # create or change workloads
    BACKUP = "backup"  # run backups and restore tests
    POWER = "power"  # start/stop/reboot guests and hosts


DISRUPTIVE = {Category.PATCH, Category.UPGRADE, Category.POWER, Category.NETWORK}


class Budget(Model):
    max_actions: int = 3
    per_minutes: int = 60
    cooldown_minutes: int = 10


class Policy(Model):
    autonomy: dict[str, Autonomy] = Field(
        default_factory=lambda: {  # type: ignore[arg-type,unused-ignore]
            Category.RESTART: Autonomy.AUTO,
            Category.RESCHEDULE: Autonomy.AUTO,
            Category.PATCH: Autonomy.AUTO,
            Category.UPGRADE: Autonomy.PROPOSE,
            Category.QUARANTINE: Autonomy.PROPOSE,
            Category.NETWORK: Autonomy.PROPOSE,
            Category.DEPLOY: Autonomy.PROPOSE,
            Category.BACKUP: Autonomy.AUTO,
            Category.POWER: Autonomy.PROPOSE,
        }
    )
    budget: Budget = Field(default_factory=Budget)
    window: Window = Field(default_factory=Window)
    kill_switch: bool = False
    require_backup_before_disruptive: bool = True


class Verdict(StrEnum):
    RUN = "run"
    WAIT_APPROVAL = "wait_approval"
    WAIT_WINDOW = "wait_window"
    RECORD_ONLY = "record_only"
    BLOCKED = "blocked"


class Decision(Model):
    verdict: Verdict
    reason: str


class Governor:
    """Applies a policy to proposed automatic actions and tracks budgets."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._history: dict[str, deque[datetime]] = defaultdict(deque)

    def decide(
        self,
        category: Category,
        target: str,
        *,
        urgent: bool = False,
        now: datetime | None = None,
    ) -> Decision:
        now = now or datetime.now(UTC)
        level = Autonomy(self.policy.autonomy.get(category, Autonomy.PROPOSE))
        if level is Autonomy.OBSERVE:
            return Decision(
                verdict=Verdict.RECORD_ONLY,
                reason=tr("Policy: report only."),
            )
        if level is Autonomy.PROPOSE:
            return Decision(
                verdict=Verdict.WAIT_APPROVAL,
                reason=tr("Policy: needs approval."),
            )
        if self.policy.kill_switch:
            return Decision(
                verdict=Verdict.WAIT_APPROVAL,
                reason=tr("The kill switch is on."),
            )
        key = f"{category}:{target}"
        history = self._history[key]
        budget = self.policy.budget
        while history and now - history[0] > timedelta(minutes=budget.per_minutes):
            history.popleft()
        if history and now - history[-1] < timedelta(minutes=budget.cooldown_minutes):
            return Decision(
                verdict=Verdict.BLOCKED,
                reason=tr("Cooldown active; needs you."),
            )
        if len(history) >= budget.max_actions:
            return Decision(
                verdict=Verdict.BLOCKED,
                reason=tr("Retry budget used up; needs you."),
            )
        if category in DISRUPTIVE and not urgent and not self.policy.window.is_open(now):
            start, _ = self.policy.window.next_open(now)
            return Decision(
                verdict=Verdict.WAIT_WINDOW,
                reason=tr("Waits for the window on {start:%a %H:%M}.", start=start),
            )
        return Decision(verdict=Verdict.RUN, reason=tr("Within policy."))

    def spend(self, category: Category, target: str, now: datetime | None = None) -> None:
        self._history[f"{category}:{target}"].append(now or datetime.now(UTC))
