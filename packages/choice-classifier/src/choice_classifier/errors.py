"""Classification failures, kept separate from successful delivery decisions."""


class ClassifierError(Exception):
    """Base class for failures handled by the integration's fallback policy."""


class InvalidInputError(ClassifierError):
    """Input has an invalid type or empty message."""


class InsufficientContextError(InvalidInputError):
    """The supplied context is empty."""


class InputTooLargeError(InvalidInputError):
    """Input exceeds the configured limit; no content was truncated."""


class InvalidResponseError(ClassifierError):
    """Provider response does not satisfy the decision contract."""


class ClassifierTimeoutError(ClassifierError):
    """The classification deadline or transport timeout expired."""


class ProviderError(ClassifierError):
    """The provider or HTTP transport failed."""

