from __future__ import annotations


class ModelInvocationError(RuntimeError):
    """A Native Agent CLI finished without a usable task result."""

    retryable = False


class ModelInvocationTimeout(ModelInvocationError):
    """A Native Agent CLI exceeded its task wall-clock budget."""

    retryable = False


class ModelInvocationCancelled(ModelInvocationError):
    """A Native Agent CLI was interrupted because its task lease was cancelled."""

    retryable = False
