# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Serve the real local setup UI with a fake Discord client for visual QA."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.notification_setup import NotificationSetupCoordinator


class _VisualClient:
    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds: float = delay_seconds

    def send(self, content: str) -> str | None:
        """Accept the safe setup test message without network access."""
        _ = content
        time.sleep(self._delay_seconds)
        return "visual-qa-message"


class _Args(argparse.Namespace):
    plugin_data: Path
    delay_seconds: float

    def __init__(self) -> None:
        """Initialize typed CLI destinations."""
        super().__init__()
        self.plugin_data = Path()
        self.delay_seconds = 0.0


def main(argv: list[str] | None = None) -> int:
    """Print one safe local URL and serve until success, expiry, or interruption."""
    parser = argparse.ArgumentParser(prog="notification-setup-visual-server")
    _ = parser.add_argument("--plugin-data", type=Path, required=True)
    _ = parser.add_argument("--delay-seconds", type=float, default=0.0)
    arguments = parser.parse_args(argv, namespace=_Args())
    if not 0.0 <= arguments.delay_seconds <= 5.0:
        parser.error("--delay-seconds must be between 0 and 5")
    coordinator = NotificationSetupCoordinator(
        arguments.plugin_data.resolve(),
        client_factory=lambda _url: _VisualClient(arguments.delay_seconds),
    )
    launch = coordinator.start()
    _ = sys.stdout.write(
        json.dumps(
            {
                "setup_url": launch.setup_url,
                "expires_in_seconds": launch.expires_in_seconds,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    _ = sys.stdout.flush()
    try:
        while coordinator.is_active():
            time.sleep(0.1)
    except KeyboardInterrupt:
        return 130
    finally:
        coordinator.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
