"""Compatibility facade for installer observation concepts."""

from scripts.installer_cache_observation import cache_matches_observation
from scripts.installer_observation_config import (
    ConfigObservation,
    disable_plugin_only,
    observe_config,
)
from scripts.installer_prior_observation import (
    PriorState,
    classify_prior,
    observation_matches_prior,
    prior_cache_still_valid,
)

__all__ = [
    "ConfigObservation",
    "PriorState",
    "cache_matches_observation",
    "classify_prior",
    "disable_plugin_only",
    "observation_matches_prior",
    "observe_config",
    "prior_cache_still_valid",
]
