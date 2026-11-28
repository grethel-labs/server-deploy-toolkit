"""Zorec's AI settings and permissions.

OpsTend works fully without AI: inventory, scans, updates, self-healing within the
policy and Zorec's rule-based answers need no model. A model adds free-form answers,
guidance on pages, drafted adapters and help in consoles.

Permissions are explicit capabilities. Each one names the functions it adds, so the
settings page can show what appears or disappears before anything is saved.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from opstend.core.models import Model
from opstend.i18n import tr
from opstend.zorec.providers import ProviderConfig


class Capability(StrEnum):
    ANSWER = "answer"  # answer free-form questions from fleet data
    EXTERNAL = "external"  # send fleet data to a provider outside your network
    GUIDE = "guide"  # navigate, highlight, fill in forms, draw diagrams
    PROPOSE = "propose"  # prepare changes as jobs for approval
    RUN = "run"  # start jobs on its own where the policy allows automation
    CONSOLE = "console"  # run commands in a console when asked with @zorec
    ADAPTERS = "adapters"  # draft adapters from repositories and test them in sandboxes
    TRIAGE = "triage"  # explain new incidents and findings in the background


DEFAULT_CAPABILITIES: dict[str, bool] = {
    Capability.ANSWER: True,
    Capability.EXTERNAL: False,
    Capability.GUIDE: True,
    Capability.PROPOSE: True,
    Capability.RUN: False,
    Capability.CONSOLE: False,
    Capability.ADAPTERS: True,
    Capability.TRIAGE: False,
}

REQUIRES: dict[str, list[str]] = {
    Capability.RUN: [Capability.PROPOSE],
    Capability.CONSOLE: [Capability.ANSWER],
    Capability.ADAPTERS: [Capability.PROPOSE],
    Capability.TRIAGE: [Capability.ANSWER],
    Capability.GUIDE: [Capability.ANSWER],
}


def catalog() -> dict[str, Any]:
    """What works without AI, and what each permission adds (rendered per request)."""
    return {
        "baseline": [
            tr("Inventory of hosts, guests, containers and the network"),
            tr("Security scans, prioritization and hardening checks"),
            tr("Updates in maintenance windows and coordinated rollouts"),
            tr("Self-healing and failover within the automation policy"),
            tr("Network insights and path explanations"),
            tr("Applications from the curated catalog with sandboxes"),
            tr(
                "Zorec answers common questions with rules (incidents, updates, where something runs)"
            ),
        ],
        "capabilities": [
            {
                "id": Capability.ANSWER,
                "label": tr("Answer questions"),
                "description": tr(
                    "Zorec answers free-form and technical questions with the model."
                ),
                "features": [
                    tr("Free-form questions about your systems"),
                    tr("Technical explanations and troubleshooting hints"),
                    tr("Follow-up questions in a conversation"),
                ],
                "risk": "low",
            },
            {
                "id": Capability.EXTERNAL,
                "label": tr("Use providers outside my network"),
                "description": tr(
                    "Names, states and findings of your systems are sent to the selected cloud provider. Secrets are never sent."
                ),
                "features": [tr("Cloud models and linked subscriptions can be used")],
                "risk": "medium",
            },
            {
                "id": Capability.GUIDE,
                "label": tr("Guide me in the app"),
                "description": tr(
                    "Zorec opens pages, highlights elements, fills in forms and draws diagrams."
                ),
                "features": [
                    tr("Opens the right page for a question"),
                    tr("Highlights the element it talks about"),
                    tr("Fills in forms for you to check and send"),
                    tr("Draws diagrams in the side panel"),
                ],
                "risk": "low",
            },
            {
                "id": Capability.PROPOSE,
                "label": tr("Prepare changes"),
                "description": tr(
                    "Zorec prepares changes as jobs. You see the preview and approve them."
                ),
                "features": [
                    tr("Restart, update or move systems after your approval"),
                    tr("Fixes for network insights and findings"),
                ],
                "risk": "low",
            },
            {
                "id": Capability.RUN,
                "label": tr("Act on its own"),
                "description": tr(
                    "Zorec starts jobs without asking where the automation policy allows it. Budgets, windows and the kill switch still apply."
                ),
                "features": [
                    tr("Fixes problems at night without waiting for you"),
                    tr("Answers become actions in categories set to automatic"),
                ],
                "risk": "high",
            },
            {
                "id": Capability.CONSOLE,
                "label": tr("Work in consoles"),
                "description": tr(
                    "In a console you can write @zorec. It proposes commands and runs them after you confirm each one."
                ),
                "features": [
                    tr("@zorec in host and service consoles"),
                    tr("Explains command output and errors"),
                ],
                "risk": "high",
            },
            {
                "id": Capability.ADAPTERS,
                "label": tr("Build adapters"),
                "description": tr(
                    "Zorec reads a repository and drafts an adapter. It runs only in a sandbox until its checks pass."
                ),
                "features": [tr("New applications from any repository")],
                "risk": "medium",
            },
            {
                "id": Capability.TRIAGE,
                "label": tr("Explain in the background"),
                "description": tr(
                    "New incidents and critical findings get a short explanation and a suggested fix."
                ),
                "features": [tr("Explanations on incidents and findings without asking")],
                "risk": "low",
            },
        ],
        "requires": REQUIRES,
    }


class Prefer(StrEnum):
    QUALITY = "quality"
    SPEED = "speed"
    LOCAL = "local"


class Selection(Model):
    """Automatic model search: tests listed models with a fixed suite in the maintenance window."""

    enabled: bool = False
    auto_switch: bool = True
    prefer: Prefer = Prefer.QUALITY
    include: list[str] = Field(default_factory=list)  # glob patterns; empty: all
    exclude: list[str] = Field(
        default_factory=lambda: ["*embed*", "*whisper*", "*tts*", "*image*", "*vision*", "*audio*"]
    )
    providers: list[str] = Field(default_factory=list)  # empty: all configured providers
    max_candidates: int = Field(default=6, ge=1, le=20)
    interval_days: int = Field(default=7, ge=1, le=90)


class AISettings(Model):
    enabled: bool = False
    providers: list[ProviderConfig] = Field(default_factory=list)
    active: str | None = None
    fallback: str | None = None
    capabilities: dict[str, bool] = Field(default_factory=lambda: dict(DEFAULT_CAPABILITIES))
    selection: Selection = Field(default_factory=Selection)

    def allowed(self, cap: Capability) -> bool:
        if not self.capabilities.get(cap, False):
            return False
        return all(self.allowed(Capability(r)) for r in REQUIRES.get(cap, []))

    def provider(self, provider_id: str | None) -> ProviderConfig | None:
        return next((p for p in self.providers if p.id == provider_id), None)

    def usable(self, provider_id: str | None) -> ProviderConfig | None:
        """The provider if AI is on and it may receive fleet data."""
        cfg = self.provider(provider_id)
        if not self.enabled or cfg is None or not self.allowed(Capability.ANSWER):
            return None
        if not cfg.local and not self.allowed(Capability.EXTERNAL):
            return None
        return cfg


def status(settings: AISettings) -> dict[str, Any]:
    """Why Zorec uses a model or not, in plain words."""
    active = settings.provider(settings.active)
    if not settings.enabled:
        return {"mode": "rules", "reason": tr("AI is off. Zorec answers with rules.")}
    if active is None:
        return {"mode": "rules", "reason": tr("No model is selected.")}
    if settings.usable(active.id) is None:
        return {
            "mode": "rules",
            "reason": tr(
                "{label} is outside your network, and sending data out is not allowed.",
                label=active.label,
            ),
        }
    return {
        "mode": "model",
        "reason": tr("{label} with {model}", label=active.label, model=active.model or "–"),
    }
