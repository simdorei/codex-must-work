"""Canonical and migration-only Codex marketplace identities."""

from __future__ import annotations

from typing import Final

PLUGIN_NAME: Final = "codex-must-work"
MARKETPLACE_NAME: Final = "simdorei"
PLUGIN_ID: Final = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
MARKETPLACE_SOURCE: Final = "https://github.com/simdorei/codex-must-work.git"
MARKETPLACE_REF: Final = "main"
DATA_ROOT_NAME: Final = f"{PLUGIN_NAME}-{MARKETPLACE_NAME}"

# The checkout installer briefly shipped this identity. It is recognized only
# so a successful simdorei installation can remove the stale selection.
LEGACY_MARKETPLACE_NAME: Final = "codex-must-work-local"
LEGACY_PLUGIN_ID: Final = f"{PLUGIN_NAME}@{LEGACY_MARKETPLACE_NAME}"
