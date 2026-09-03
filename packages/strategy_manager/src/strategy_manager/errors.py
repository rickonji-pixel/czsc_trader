class StrategyManagerError(Exception):
    """Base class for stable Strategy Manager domain errors."""


class ValidationError(StrategyManagerError):
    """Raised when a domain object violates its schema."""


class RegistryError(StrategyManagerError):
    """Raised when registry persistence or identity resolution fails."""


class ImmutableVersionError(RegistryError):
    """Raised when a frozen strategy version no longer matches its release hash."""


class InvalidTransitionError(RegistryError):
    """Raised when a lifecycle transition is not allowed."""


class EvidenceRequiredError(RegistryError):
    """Raised when a lifecycle transition lacks required evidence."""
