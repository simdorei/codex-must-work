"""Fail-safe capability gates for warning and restart control."""

from __future__ import annotations

import hashlib
import platform
from dataclasses import dataclass
from typing import Final

PLUGIN_VERSION: Final = "0.1.0"


@dataclass(frozen=True, slots=True)
class CapabilityInputs:
    """Verified control capabilities tied to one environment fingerprint."""

    event_stream_ready: bool
    same_live_server_attach: bool
    targeted_interrupt: bool
    reassign_existing_task: bool
    generation_guard: bool
    fingerprint: str


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    """Effective actions and the stable reason for disabled behavior."""

    warning_delivery_ready: bool
    auto_restart_ready: bool
    reason_code: str
    evidence_fingerprint: str
    stop_continuation_ready: bool = False


def evaluate_capabilities(inputs: CapabilityInputs) -> CapabilityReport:
    """Enable an action only when all evidence required for it is present."""
    if not inputs.fingerprint.strip():
        return CapabilityReport(
            warning_delivery_ready=False,
            auto_restart_ready=False,
            reason_code="capability_evidence_missing",
            evidence_fingerprint="",
        )
    if not inputs.event_stream_ready:
        return CapabilityReport(
            warning_delivery_ready=False,
            auto_restart_ready=False,
            reason_code="event_stream_unavailable",
            evidence_fingerprint=inputs.fingerprint,
        )
    if not inputs.same_live_server_attach:
        return CapabilityReport(
            warning_delivery_ready=False,
            auto_restart_ready=False,
            reason_code="same_live_server_attach_unavailable",
            evidence_fingerprint=inputs.fingerprint,
        )
    restart_ready = (
        inputs.targeted_interrupt and inputs.reassign_existing_task and inputs.generation_guard
    )
    return CapabilityReport(
        warning_delivery_ready=True,
        auto_restart_ready=restart_ready,
        reason_code="ready" if restart_ready else "auto_restart_controls_unavailable",
        evidence_fingerprint=inputs.fingerprint,
    )


def current_capabilities(
    codex_version: str,
    desktop_build: str = "unavailable",
    control_api_schema: str = "unavailable",
) -> CapabilityReport:
    """Return the conservative capabilities of the currently probed runtime."""
    fingerprint_source = (
        f"{platform.system()}|{platform.release()}|{codex_version}|{desktop_build}|"
        f"{PLUGIN_VERSION}|{control_api_schema}"
    )
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
    report = evaluate_capabilities(
        CapabilityInputs(
            event_stream_ready=True,
            same_live_server_attach=False,
            targeted_interrupt=False,
            reassign_existing_task=False,
            generation_guard=False,
            fingerprint=fingerprint,
        )
    )
    return CapabilityReport(
        warning_delivery_ready=report.warning_delivery_ready,
        auto_restart_ready=report.auto_restart_ready,
        reason_code=report.reason_code,
        evidence_fingerprint=report.evidence_fingerprint,
        stop_continuation_ready=True,
    )
