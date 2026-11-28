"""One job model for every change.

Every change, whether a person or the automation starts it, is a job with the
same lifecycle:

    preview -> (approval) -> run -> verify (readback) -> done
                                \\-> rollback on failure

A preview records a fingerprint of the state it was computed from. Approving
a stale preview is refused, so nobody approves a change against facts that no
longer hold. Each job carries the leader term that admitted it; a deposed
leader cannot execute steps (fencing).
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import Field

from opstend.core.audit import AuditLog
from opstend.core.models import Localized, utcnow
from opstend.core.store import Store


class JobState(StrEnum):
    PREVIEWED = "previewed"
    AWAITING_APPROVAL = "awaiting_approval"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


TERMINAL = {
    JobState.SUCCEEDED,
    JobState.FAILED,
    JobState.ROLLED_BACK,
    JobState.REJECTED,
    JobState.CANCELLED,
}


class StepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


class Step(Localized):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    title: str = ""
    state: StepState = StepState.PENDING
    effect: str = ""
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Job(Localized):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str
    category: str
    target: str
    reason: str = ""
    requested_by: str = "system"
    state: JobState = JobState.PREVIEWED
    steps: list[Step] = Field(default_factory=list)
    fingerprint: str = ""
    approved_by: str | None = None
    term: int = 0
    idempotency_key: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    not_before: datetime | None = None
    message: str = ""
    zorec_summary: str | None = None


class JobError(RuntimeError):
    pass


@dataclass
class Context:
    """What an action can use while it runs."""

    job: Job
    store: Store
    log: Callable[[str], None]


class Action(Protocol):
    name: str
    category: str

    async def preview(self, params: dict[str, Any], ctx: Context) -> str:
        """Describe the effect in one sentence, without changing anything."""

    async def run(self, params: dict[str, Any], ctx: Context) -> dict[str, Any]: ...

    async def verify(self, params: dict[str, Any], ctx: Context, result: dict[str, Any]) -> bool:
        """Read back the target and confirm the intended state."""

    async def rollback(
        self, params: dict[str, Any], ctx: Context, result: dict[str, Any]
    ) -> None: ...

    def fingerprint(self, params: dict[str, Any], store: Store) -> Any:
        """Facts the preview depends on. A change invalidates approval."""


class ActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, Action] = {}

    def register(self, action: Action) -> None:
        if action.name in self._actions:
            raise ValueError(f"action {action.name} already registered")
        self._actions[action.name] = action

    def get(self, name: str) -> Action:
        try:
            return self._actions[name]
        except KeyError:
            raise JobError(f"unknown action {name!r}") from None

    def names(self) -> list[str]:
        return sorted(self._actions)


class Engine:
    """Creates, approves and runs jobs. Only the current leader runs jobs."""

    def __init__(
        self,
        store: Store,
        audit: AuditLog,
        registry: ActionRegistry,
        term: Callable[[], int] = lambda: 1,
        is_leader: Callable[[], bool] = lambda: True,
        writer: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
    ) -> None:
        self.store = store
        self._writer = writer
        self.audit = audit
        self.registry = registry
        self._term = term
        self._is_leader = is_leader
        self._listeners: list[Callable[[Job], Awaitable[None] | None]] = []
        self._locks: dict[str, asyncio.Lock] = {}

    def on_change(self, listener: Callable[[Job], Awaitable[None] | None]) -> None:
        self._listeners.append(listener)

    # -- persistence -----------------------------------------------------

    def get(self, job_id: str) -> Job:
        data = self.store.get("job", job_id)
        if data is None:
            raise JobError(f"job {job_id} not found")
        return Job.model_validate(data)

    def list(self, *, open_only: bool = False) -> builtins.list[Job]:
        jobs = [Job.model_validate(d) for d in self.store.list("job")]
        if open_only:
            jobs = [j for j in jobs if j.state not in TERMINAL]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    async def _save(self, job: Job) -> None:
        job.updated_at = utcnow()
        job.remember_texts("title", "reason", "message")
        for step in job.steps:
            step.remember_texts("title", "effect", "error")
        if self._writer is not None:
            # Replicated: the write returns once a majority stored it.
            await self._writer(
                {"op": "put", "kind": "job", "id": job.id, "data": job.model_dump(mode="json")}
            )
        else:
            self.store.put("job", job.id, job)
        for listener in self._listeners:
            result = listener(job)
            if asyncio.iscoroutine(result):
                await result

    # -- lifecycle -------------------------------------------------------

    def _fingerprint(self, job: Job) -> str:
        facts = []
        for step in job.steps:
            action = self.registry.get(step.action)
            facts.append([step.action, action.fingerprint(step.params, self.store)])
        blob = json.dumps(facts, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    async def preview(self, job: Job, *, auto_approve: bool = False) -> Job:
        if job.idempotency_key:
            for existing in self.list(open_only=True):
                if existing.idempotency_key == job.idempotency_key:
                    return existing
        ctx = Context(job=job, store=self.store, log=lambda _m: None)
        for step in job.steps:
            action = self.registry.get(step.action)
            step.effect = await action.preview(step.params, ctx)
            if not step.title:
                step.title = step.effect
        job.fingerprint = self._fingerprint(job)
        job.state = JobState.SCHEDULED if auto_approve else JobState.AWAITING_APPROVAL
        if auto_approve:
            job.approved_by = "policy"
        await self._save(job)
        self.audit.record(
            job.requested_by,
            "job.preview",
            job.target,
            job_id=job.id,
            detail={"title": job.title, "auto": auto_approve},
        )
        return job

    async def approve(self, job_id: str, actor: str, fingerprint: str | None = None) -> Job:
        job = self.get(job_id)
        if job.state != JobState.AWAITING_APPROVAL:
            raise JobError(f"job is {job.state}, not awaiting approval")
        current = self._fingerprint(job)
        if fingerprint is not None and fingerprint != job.fingerprint:
            raise JobError("approval refers to a different preview")
        if current != job.fingerprint:
            job.state = JobState.REJECTED
            job.message = "The target changed after the preview. Create a new preview."
            await self._save(job)
            self.audit.record(actor, "job.stale", job.target, "refused", job.id)
            raise JobError(job.message)
        job.state = JobState.SCHEDULED
        job.approved_by = actor
        await self._save(job)
        self.audit.record(actor, "job.approve", job.target, job_id=job.id)
        return job

    async def reject(self, job_id: str, actor: str, reason: str = "") -> Job:
        job = self.get(job_id)
        if job.state in TERMINAL or job.state == JobState.RUNNING:
            raise JobError(f"job is {job.state}")
        job.state = JobState.REJECTED
        job.message = reason
        await self._save(job)
        self.audit.record(actor, "job.reject", job.target, job_id=job.id, detail={"reason": reason})
        return job

    async def run(self, job_id: str) -> Job:
        lock = self._locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            job = self.get(job_id)
            if job.state != JobState.SCHEDULED:
                return job
            if not self._is_leader():
                raise JobError("only the leader runs jobs")
            job.term = self._term()
            job.state = JobState.RUNNING
            await self._save(job)
            self.audit.record("opstend", "job.start", job.target, job_id=job.id)
            completed: list[tuple[Step, dict[str, Any]]] = []
            ctx = Context(job=job, store=self.store, log=lambda m: None)
            for step in job.steps:
                if self._term() != job.term or not self._is_leader():
                    job.state = JobState.FAILED
                    job.message = "Leadership changed during the job. The new leader reconciles it."
                    await self._save(job)
                    return job
                action = self.registry.get(step.action)
                step.state = StepState.RUNNING
                step.started_at = datetime.now(UTC)
                await self._save(job)
                try:
                    result = await action.run(step.params, ctx)
                    step.output = result
                    job.state = JobState.VERIFYING
                    await self._save(job)
                    if not await action.verify(step.params, ctx, result):
                        raise JobError("readback did not confirm the intended state")
                    job.state = JobState.RUNNING
                    step.state = StepState.DONE
                    completed.append((step, result))
                    self.audit.record(
                        "opstend",
                        step.action,
                        job.target,
                        job_id=job.id,
                        detail={"effect": step.effect},
                    )
                except Exception as exc:
                    step.state = StepState.FAILED
                    step.error = str(exc)
                    step.finished_at = datetime.now(UTC)
                    self.audit.record(
                        "opstend", step.action, job.target, "failed", job.id, {"error": str(exc)}
                    )
                    await self._rollback(job, completed, ctx)
                    return job
                step.finished_at = datetime.now(UTC)
            job.state = JobState.SUCCEEDED
            job.message = "Done and confirmed by readback."
            await self._save(job)
            self.audit.record("opstend", "job.succeeded", job.target, job_id=job.id)
            return job

    async def _rollback(
        self, job: Job, completed: builtins.list[tuple[Step, dict[str, Any]]], ctx: Context
    ) -> None:
        rolled = True
        for step, result in reversed(completed):
            try:
                await self.registry.get(step.action).rollback(step.params, ctx, result)
                step.state = StepState.ROLLED_BACK
            except Exception as exc:
                rolled = False
                step.error = f"rollback failed: {exc}"
        for step in job.steps:
            if step.state == StepState.PENDING:
                step.state = StepState.SKIPPED
        job.state = JobState.ROLLED_BACK if completed and rolled else JobState.FAILED
        job.message = (
            "A step failed. Completed steps were rolled back."
            if job.state == JobState.ROLLED_BACK
            else "A step failed. Check the target; manual follow-up may be needed."
        )
        await self._save(job)
        self.audit.record("opstend", "job." + job.state, job.target, "failed", job.id)

    async def run_due(self, now: datetime | None = None) -> builtins.list[Job]:
        now = now or datetime.now(UTC)
        ran = []
        for job in self.list(open_only=True):
            if job.state == JobState.SCHEDULED and (
                job.not_before is None or job.not_before <= now
            ):
                ran.append(await self.run(job.id))
        return ran


class FunctionAction:
    """Build an action from plain callables. Used by adapters and tests."""

    def __init__(
        self,
        name: str,
        category: str,
        *,
        describe: Callable[[dict[str, Any]], str],
        execute: Callable[[dict[str, Any], Context], Awaitable[dict[str, Any]]],
        check: Callable[[dict[str, Any], Context, dict[str, Any]], Awaitable[bool]] | None = None,
        undo: Callable[[dict[str, Any], Context, dict[str, Any]], Awaitable[None]] | None = None,
        facts: Callable[[dict[str, Any], Store], Any] | None = None,
    ) -> None:
        self.name = name
        self.category = category
        self._describe = describe
        self._execute = execute
        self._check = check
        self._undo = undo
        self._facts = facts

    async def preview(self, params: dict[str, Any], ctx: Context) -> str:
        return self._describe(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> dict[str, Any]:
        return await self._execute(params, ctx)

    async def verify(self, params: dict[str, Any], ctx: Context, result: dict[str, Any]) -> bool:
        return True if self._check is None else await self._check(params, ctx, result)

    async def rollback(self, params: dict[str, Any], ctx: Context, result: dict[str, Any]) -> None:
        if self._undo is not None:
            await self._undo(params, ctx, result)

    def fingerprint(self, params: dict[str, Any], store: Store) -> Any:
        return None if self._facts is None else self._facts(params, store)
