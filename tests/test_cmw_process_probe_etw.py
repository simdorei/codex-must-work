from __future__ import annotations

import pytest

from tests.cmw_process_probe_etw import (
    EtwProviderError,
    parse_loss_counters,
    provider_file_text,
)


def test_provider_file_enables_process_and_wmi_activity_only() -> None:
    # Given / When
    text = provider_file_text()

    # Then
    assert "Microsoft-Windows-Kernel-Process 0x10 0x4" in text
    assert "Microsoft-Windows-WMI-Activity 0x8000000000000000 0x4" in text
    assert "Win32_Process" not in text
    assert "wmic" not in text.lower()


def test_loss_parser_records_session_and_each_provider() -> None:
    # Given
    output = """
Events Lost : 0
Buffers Lost : 0
Real Time Buffers Lost : 0
"""

    # When
    counters = parse_loss_counters(output)

    # Then
    assert counters.events_lost == 0
    assert counters.buffers_lost == 0
    assert counters.provider_losses is None


def test_loss_parser_rejects_misleading_output_without_loss_fields() -> None:
    # Given
    output = "The command completed successfully."

    # When / Then
    with pytest.raises(EtwProviderError, match="etw_loss_counters_unavailable"):
        _ = parse_loss_counters(output)
