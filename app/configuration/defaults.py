"""Versioned starting values; existing tenant documents are never overlaid at runtime."""

from app.configuration.schemas import OperationalConfiguration
from app.conversation.locales import LanguagePolicy

DEFAULTS_VERSION = 2
DEFAULT_LOCATION_NAME = "Main location"


def tenant_defaults() -> OperationalConfiguration:
    # Each caller owns a new deep object. No tenant can mutate a shared default instance.
    return OperationalConfiguration()


def initial_language_policy() -> LanguagePolicy:
    return tenant_defaults().languages
