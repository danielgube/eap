class EapError(Exception):
    """Expected error that can be shown without a traceback."""


class ValidationError(EapError):
    """Configuration, catalog or state validation failed."""


class NetworkError(EapError):
    """A remote resource could not be resolved or downloaded."""


class IntegrityError(EapError):
    """A downloaded or installed artifact failed verification."""


class TransactionError(EapError):
    """A filesystem transaction could not be completed safely."""
