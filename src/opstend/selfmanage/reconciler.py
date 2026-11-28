"""Self-management: compare what should be with what is, then act within policy.

Every cycle the reconciler turns observations into proposals:

* a service fails its probe repeatedly -> restart it; after the budget is
  spent, escalate to the operator,
* a node is down -> move its roles and restartable workloads elsewhere,
* a known exploited vulnerability is reachable from outside -> withdraw the
  exposure (quarantine) and patch urgently,
* security updates are pending -> patch in the next maintenance window,
* a backup is older than its target -> run it.

The governor decides per proposal whether it runs now, waits for the window,
waits for approval or is only recorded. Each outcome is visible as an incident
or decision in the UI, with the reason in plain words.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from opstend.core.jobs import Engine, Job, Step
from opstend.core.models import Finding, Node, NodeState, Service, Severity, Update
from opstend.core.policy import Category, Governor, Verdict
from opstend.i18n import tr
from opstend.selfmanage.health import ProbeResult


@dataclass
class Proposal:
    category: Category
    target: str
    title: str
    reason: str
    steps: list[Step]
    urgent: bool = False
    key: str = ""


@dataclass
class Incident:
    id: str
    target: str
    title: str
    detail: str
    severity: Severity
    state: str  # handling | waiting | needs_operator | resolved
    job_id: str | None = None
    since: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Observations:
    services: list[Service] = field(default_factory=list)
    probes: dict[str, list[ProbeResult]] = field(default_factory=dict)  # newest last
    nodes: list[Node] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    updates: list[Update] = field(default_factory=list)
    last_backup: dict[str, datetime] = field(default_factory=dict)
    backup_targets: dict[str, timedelta] = field(default_factory=dict)
    publications: dict[str, str] = field(default_factory=dict)  # service id -> publication id
    names: dict[str, str] = field(default_factory=dict)  # object id -> display name

    def name(self, oid: str) -> str:
        return self.names.get(oid, oid.split(":")[-1].split("/")[-1])


class Reconciler:
    def __init__(self, governor: Governor, engine: Engine, failures_before_action: int = 3) -> None:
        self.governor = governor
        self.engine = engine
        self.failures = failures_before_action

    def propose(self, obs: Observations, now: datetime | None = None) -> list[Proposal]:
        now = now or datetime.now(UTC)
        out: list[Proposal] = []
        for svc in obs.services:
            history = obs.probes.get(svc.id, [])
            recent = history[-self.failures :]
            if len(recent) == self.failures and all(r == ProbeResult.UNHEALTHY for r in recent):
                out.append(
                    Proposal(
                        Category.RESTART,
                        f"service/{svc.id}",
                        tr("Restart {svc_name}", svc_name=svc.name),
                        tr("{failures} health checks in a row failed", failures=self.failures),
                        [Step(action="service.restart", params={"service": svc.id})],
                        key=f"restart:{svc.id}",
                    )
                )
        for node in obs.nodes:
            if NodeState(node.state) == NodeState.DOWN:
                out.append(
                    Proposal(
                        Category.RESCHEDULE,
                        f"node/{node.id}",
                        tr("Move roles off {node_name}", node_name=node.name),
                        tr(
                            "{node_name} has not reported since {node_last_seen:%H:%M}",
                            node_name=node.name,
                            node_last_seen=node.last_seen,
                        ),
                        [Step(action="cluster.replace_node", params={"node": node.id})],
                        urgent=True,
                        key=f"reschedule:{node.id}",
                    )
                )
        for f in obs.findings:
            if f.kev and f.exposed and f.resolved_at is None:
                service = f.target.removeprefix("service/")
                if service in obs.publications:
                    out.append(
                        Proposal(
                            Category.QUARANTINE,
                            f.target,
                            tr(
                                "Withdraw public access to {obs_name_service}",
                                obs_name_service=obs.name(service),
                            ),
                            tr(
                                "{f_cve} is known to be exploited and the service is reachable from the internet",
                                f_cve=f.cve,
                            ),
                            [
                                Step(
                                    action="publication.withdraw",
                                    params={"publication": obs.publications[service]},
                                )
                            ],
                            urgent=True,
                            key=f"quarantine:{service}",
                        )
                    )
        security_by_target: dict[str, list[Update]] = defaultdict(list)
        for u in obs.updates:
            if u.security:
                security_by_target[u.target].append(u)
        for target, ups in security_by_target.items():
            urgent = any(f.kev and f.target == target for f in obs.findings)
            out.append(
                Proposal(
                    Category.PATCH,
                    target,
                    tr(
                        "Install {len_ups} security updates on {obs_name_target}",
                        len_ups=len(ups),
                        obs_name_target=obs.name(target),
                    ),
                    tr("Security updates are available and fix a known exploited issue")
                    if urgent
                    else tr("Security updates are available"),
                    [Step(action="host.patch", params={"host": target, "security_only": True})],
                    urgent=urgent,
                    key=f"patch:{target}",
                )
            )
        for target, max_age in obs.backup_targets.items():
            last = obs.last_backup.get(target)
            if last is None or now - last > max_age:
                out.append(
                    Proposal(
                        Category.BACKUP,
                        target,
                        tr("Back up {obs_name_target}", obs_name_target=obs.name(target)),
                        tr("no backup yet")
                        if last is None
                        else tr("last backup {last:%Y-%m-%d %H:%M}", last=last),
                        [Step(action="backup.run", params={"target": target})],
                        key=f"backup:{target}",
                    )
                )
        return out

    async def act(self, proposals: list[Proposal], now: datetime | None = None) -> list[Incident]:
        now = now or datetime.now(UTC)
        incidents: list[Incident] = []
        for p in proposals:
            decision = self.governor.decide(p.category, p.target, urgent=p.urgent, now=now)
            verdict = Verdict(decision.verdict)
            job = Job(
                title=p.title,
                category=p.category,
                target=p.target,
                reason=p.reason,
                steps=p.steps,
                idempotency_key=p.key or None,
            )
            if verdict == Verdict.RUN:
                job = await self.engine.preview(job, auto_approve=True)
                self.governor.spend(p.category, p.target, now)
                state = "handling"
            elif verdict == Verdict.WAIT_WINDOW:
                job.not_before = self.governor.policy.window.next_open(now)[0]
                job = await self.engine.preview(job, auto_approve=True)
                state = "waiting"
            elif verdict == Verdict.WAIT_APPROVAL:
                job = await self.engine.preview(job)
                state = "waiting"
            elif verdict == Verdict.BLOCKED:
                incidents.append(
                    Incident(
                        id=p.key or p.target,
                        target=p.target,
                        title=p.title,
                        detail=f"{p.reason}. {decision.reason}",
                        severity=Severity.HIGH,
                        state="needs_operator",
                    )
                )
                continue
            else:
                incidents.append(
                    Incident(
                        id=p.key or p.target,
                        target=p.target,
                        title=p.title,
                        detail=f"{p.reason}. {decision.reason}",
                        severity=Severity.MEDIUM,
                        state="waiting",
                    )
                )
                continue
            incidents.append(
                Incident(
                    id=p.key or job.id,
                    target=p.target,
                    title=p.title,
                    detail=f"{p.reason}. {decision.reason}",
                    severity=Severity.HIGH if p.urgent else Severity.MEDIUM,
                    state=state,
                    job_id=job.id,
                )
            )
        return incidents
