"""Контекст запроса/события: correlationId попадает во все строки лога (HTTP и обработчики событий)."""
from contextvars import ContextVar

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")
