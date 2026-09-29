"""Map exceptions to a small, safe, low-cardinality category vocabulary.

Telemetry must never carry exception messages: those can embed SQL, file names,
document text, or provider internals. Only the category below is recorded.
Matching is by class name along the exception's MRO so this package does not
import application error types.
"""

from __future__ import annotations

ERROR_CATEGORIES = (
    "unsafe_query",
    "timeout",
    "provider_configuration",
    "provider_failure",
    "sql_generation",
    "retrieval_or_grounding",
    "invalid_input",
    "not_found",
    "conflict",
    "dependency",
    "internal",
)

_NAME_CATEGORIES: dict[str, str] = {
    "UnsafeQueryError": "unsafe_query",
    "QueryTimeoutError": "timeout",
    "TimeoutError": "timeout",
    "LLMConfigurationError": "provider_configuration",
    "LLMGenerationError": "provider_failure",
    "SQLAgentError": "sql_generation",
    "RAGAgentError": "retrieval_or_grounding",
    "PDFLoadError": "invalid_input",
    "TabularLoadError": "invalid_input",
    "TabularWorkflowError": "invalid_input",
    "ResourceNotFoundError": "not_found",
    "ResourceConflictError": "conflict",
    "EmbeddingError": "dependency",
    "ValueError": "invalid_input",
}


def error_category(exc: BaseException) -> str:
    """Return a safe category for an exception; unknown types are ``internal``."""
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        if status_code == 404:
            return "not_found"
        if status_code == 409:
            return "conflict"
        if 400 <= status_code < 500:
            return "invalid_input"
        return "internal"
    for cls in type(exc).__mro__:
        category = _NAME_CATEGORIES.get(cls.__name__)
        if category is not None:
            return category
    return "internal"
