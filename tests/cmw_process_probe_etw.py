"""Windows ETW provider configuration and loss-counter parsing."""

from __future__ import annotations

import re
from typing import Final, final

from tests.cmw_process_probe_models import LossCounters

_PROCESS_PROVIDER: Final = "Microsoft-Windows-Kernel-Process"
_WMI_PROVIDER: Final = "Microsoft-Windows-WMI-Activity"
_LOSS_PATTERN: Final = re.compile(
    r"(?im)^\s*(Events Lost|Buffers Lost|Real Time Buffers Lost)\s*:\s*(\d+)\s*$"
)


@final
class EtwProviderError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def provider_file_text() -> str:
    """Return the exact two-provider configuration without querying WMI."""
    return f"{_PROCESS_PROVIDER} 0x10 0x4\n{_WMI_PROVIDER} 0x8000000000000000 0x4\n"


def parse_loss_counters(output: str) -> LossCounters:
    """Require explicit session loss fields before claiming provider loss-free coverage."""
    matches: list[tuple[str, str]] = _LOSS_PATTERN.findall(output)
    values = {name.casefold(): int(value) for name, value in matches}
    events = values.get("events lost")
    buffers = values.get("buffers lost")
    realtime = values.get("real time buffers lost")
    if events is None or buffers is None or realtime is None:
        reason = "etw_loss_counters_unavailable"
        raise EtwProviderError(reason)
    total_buffers = buffers + realtime
    return LossCounters(
        events_lost=events,
        buffers_lost=total_buffers,
        provider_losses=None,
    )
