"""A7 - Input validation, rate limit and retry middleware (Epic A: Story FD-4).

Provides three core reliability and guardrail primitives for Retail AI Agents:
1. Input Validation (`InputValidator`):
   - Enforces strict positive-integer checks on quantities (blocking booleans,
     zero, negatives, floats, and non-numeric strings as required by RS-5 / B8
     and Restocking guardrails).
   - Validates branch IDs, product IDs, non-negative Decimal prices/costs,
     date ranges (`expiration_date >= production_date`), and required strings.
2. Rate Limiting (`SlidingWindowRateLimiter`):
   - Bounds request rates per `(branch_id, action)` or `actor_id` over a configurable
     time window to prevent runaway agent loops or duplicate scheduler bursts.
3. Bounded Retries (`BoundedRetryPolicy`):
   - Enforces a strict `max_retries` bound (linked to `ConfigService.get_max_retries()`)
     for both transient tool/API execution and human-in-the-loop rejection loops (RS-8 / B12).
   - Fails fast without retrying on `InputValidationError` or `RateLimitExceededError`.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import time
from typing import Any, Callable, Deque, Dict, FrozenSet, Mapping, Optional, Tuple, Type, TypeVar

from foundation.config_service import ConfigService

T = TypeVar("T")


class GuardrailError(ValueError):
    """Base exception for input validation, rate limiting, and retry guardrails."""


class InputValidationError(GuardrailError):
    """Raised when an input payload fails schema or domain validation rules."""

    def __init__(self, message: str, *, field_name: Optional[str] = None) -> None:
        super().__init__(message)
        self.field_name = field_name


class RateLimitExceededError(GuardrailError):
    """Raised when an actor or branch exceeds the allowed request rate."""


class RetryLimitExceededError(GuardrailError):
    """Raised when an operation or rejection loop exceeds `max_retries`."""

    def __init__(
        self,
        message: str,
        *,
        attempts_made: int,
        max_retries: int,
        last_error: Optional[ BaseException ] = None,
    ) -> None:
        super().__init__(message)
        self.attempts_made = attempts_made
        self.max_retries = max_retries
        self.last_error = last_error


VALID_DEMAND_LEVELS: FrozenSet[str] = frozenset({"Low", "Medium", "High"})


# ---------------------------------------------------------------------------
# 1. Input Validation Middleware
# ---------------------------------------------------------------------------
class InputValidator:
    """Deterministic input validator for workflow payloads and tool parameters (Task A7)."""

    @staticmethod
    def validate_positive_int(value: Any, field_name: str = "quantity") -> int:
        """Ensures `value` is a strict positive integer (> 0).

        Rejects booleans (`True`/`False` are subclasses of `int` in Python),
        floats (`3.5` or `3.0`), strings (`"10"`), zero, and negative numbers.
        """
        if isinstance(value, bool) or not isinstance(value, int):
            raise InputValidationError(
                f"Field '{field_name}' must be a strict integer, got {type(value).__name__} ({value!r}).",
                field_name=field_name,
            )
        if value <= 0:
            raise InputValidationError(
                f"Field '{field_name}' must be a positive integer (> 0), got {value}.",
                field_name=field_name,
            )
        return value

    @staticmethod
    def validate_non_negative_int(value: Any, field_name: str = "stock_quantity") -> int:
        """Ensures `value` is a strict non-negative integer (>= 0)."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise InputValidationError(
                f"Field '{field_name}' must be a strict integer, got {type(value).__name__} ({value!r}).",
                field_name=field_name,
            )
        if value < 0:
            raise InputValidationError(
                f"Field '{field_name}' must be non-negative (>= 0), got {value}.",
                field_name=field_name,
            )
        return value

    @staticmethod
    def validate_non_empty_str(value: Any, field_name: str, *, max_length: int = 500) -> str:
        """Ensures `value` is a non-empty string within `max_length`."""
        if not isinstance(value, str):
            raise InputValidationError(
                f"Field '{field_name}' must be a string, got {type(value).__name__}.",
                field_name=field_name,
            )
        cleaned = value.strip()
        if not cleaned:
            raise InputValidationError(
                f"Field '{field_name}' cannot be empty or whitespace.",
                field_name=field_name,
            )
        if len(cleaned) > max_length:
            raise InputValidationError(
                f"Field '{field_name}' exceeds maximum length of {max_length} characters.",
                field_name=field_name,
            )
        return cleaned

    @staticmethod
    def validate_decimal_money(
        value: Any,
        field_name: str,
        *,
        allow_zero: bool = True,
    ) -> Decimal:
        """Ensures `value` converts cleanly to a finite non-negative `Decimal`."""
        if isinstance(value, bool) or value is None or isinstance(value, float):
            raise InputValidationError(
                f"Field '{field_name}' must be Decimal, int, or numeric string (not float/bool/None).",
                field_name=field_name,
            )
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise InputValidationError(
                f"Field '{field_name}' is not a valid decimal number: {value!r}.",
                field_name=field_name,
            ) from exc

        if not dec.is_finite():
            raise InputValidationError(
                f"Field '{field_name}' must be a finite decimal, got {value!r}.",
                field_name=field_name,
            )
        if allow_zero and dec < Decimal("0"):
            raise InputValidationError(
                f"Field '{field_name}' cannot be negative ({dec}).",
                field_name=field_name,
            )
        if not allow_zero and dec <= Decimal("0"):
            raise InputValidationError(
                f"Field '{field_name}' must be strictly positive ({dec}).",
                field_name=field_name,
            )
        return dec

    @staticmethod
    def validate_date_order(
        production_date: date | str,
        expiration_date: date | str,
    ) -> Tuple[date, date]:
        """Validates that `expiration_date` is on or after `production_date`."""
        def _to_date(val: Any, name: str) -> date:
            if isinstance(val, datetime):
                return val.date()
            if isinstance(val, date):
                return val
            if isinstance(val, str):
                try:
                    return date.fromisoformat(val.strip())
                except ValueError as exc:
                    raise InputValidationError(
                        f"Field '{name}' must be ISO YYYY-MM-DD date string, got {val!r}.",
                        field_name=name,
                    ) from exc
            raise InputValidationError(
                f"Field '{name}' must be a date or ISO string.",
                field_name=name,
            )

        prod_dt = _to_date(production_date, "production_date")
        exp_dt = _to_date(expiration_date, "expiration_date")
        if exp_dt < prod_dt:
            raise InputValidationError(
                f"expiration_date ({exp_dt}) cannot be earlier than production_date ({prod_dt}).",
                field_name="expiration_date",
            )
        return prod_dt, exp_dt

    @classmethod
    def validate_restock_order_input(cls, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Validates a restock order draft payload before presenting to HumanReviewNode."""
        if not isinstance(payload, Mapping):
            raise InputValidationError("Restock order payload must be a dictionary/mapping.")

        branch_id = cls.validate_positive_int(payload.get("branch_id"), "branch_id")
        product_id = cls.validate_positive_int(payload.get("product_id"), "product_id")
        supplier_id = cls.validate_positive_int(payload.get("supplier_id"), "supplier_id")
        requested_quantity = cls.validate_positive_int(
            payload.get("requested_quantity"), "requested_quantity"
        )
        run_id = cls.validate_non_empty_str(payload.get("run_id"), "run_id", max_length=128)

        demand_raw = payload.get("demand_level", "Medium")
        demand_str = cls.validate_non_empty_str(demand_raw, "demand_level").capitalize()
        if demand_str not in VALID_DEMAND_LEVELS:
            raise InputValidationError(
                f"Invalid demand_level '{demand_raw}'. Must be one of {sorted(VALID_DEMAND_LEVELS)}.",
                field_name="demand_level",
            )

        validated = dict(payload)
        validated.update(
            {
                "branch_id": branch_id,
                "product_id": product_id,
                "supplier_id": supplier_id,
                "requested_quantity": requested_quantity,
                "run_id": run_id,
                "demand_level": demand_str,
            }
        )
        return validated


# ---------------------------------------------------------------------------
# 2. Rate Limit Middleware
# ---------------------------------------------------------------------------
class SlidingWindowRateLimiter:
    """In-memory sliding-window rate limiter scoped by key (e.g. branch_id or actor_id)."""

    def __init__(
        self,
        *,
        max_requests: int = 30,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_requests <= 0:
            raise ValueError("max_requests must be a positive integer.")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive.")
        self._max_requests = int(max_requests)
        self._window_seconds = float(window_seconds)
        self._clock = clock
        self._buckets: Dict[str, Deque[float]] = {}

    def check_and_record(self, key: str) -> int:
        """Checks if `key` is within rate limits and records the request timestamp.

        Returns the number of remaining requests in the current window, or raises
        `RateLimitExceededError` if the limit has been reached.
        """
        cleaned_key = InputValidator.validate_non_empty_str(key, "rate_limit_key", max_length=200)
        now = float(self._clock())
        cutoff = now - self._window_seconds

        bucket = self._buckets.setdefault(cleaned_key, deque())
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= self._max_requests:
            raise RateLimitExceededError(
                f"Rate limit exceeded for '{cleaned_key}': max {self._max_requests} "
                f"requests per {self._window_seconds:.1f}s window."
            )

        bucket.append(now)
        return self._max_requests - len(bucket)


# ---------------------------------------------------------------------------
# 3. Bounded Retry Middleware (Execution & HITL Rejection Loop)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RetryOutcome:
    """Result returned by `BoundedRetryPolicy.execute`."""
    result: Any
    attempts_made: int
    retries_used: int


class BoundedRetryPolicy:
    """Enforces bounded retries for transient errors and HITL rejection loops (Task A7)."""

    def __init__(
        self,
        *,
        max_retries: Optional[int] = None,
        config_service: Optional[ConfigService] = None,
        non_retryable_exceptions: Tuple[Type[BaseException], ...] = (
            InputValidationError,
            RateLimitExceededError,
            PermissionError,
        ),
        sleep_fn: Callable[[float], None] = lambda _: None,
        base_backoff_seconds: float = 0.0,
    ) -> None:
        self._config = config_service if config_service is not None else ConfigService()
        if max_retries is not None and max_retries < 0:
            raise ValueError("max_retries must be non-negative.")
        self._explicit_max_retries = max_retries
        self._non_retryable = non_retryable_exceptions
        self._sleep_fn = sleep_fn
        self._base_backoff = float(base_backoff_seconds)

    def resolve_max_retries(self, branch_id: int = 0) -> int:
        if self._explicit_max_retries is not None:
            return self._explicit_max_retries
        return self._config.get_max_retries(branch_id=branch_id)

    def execute(
        self,
        operation: Callable[[], T],
        *,
        branch_id: int = 0,
        operation_name: str = "workflow_step",
    ) -> RetryOutcome:
        """Executes `operation` at most `1 + max_retries` times.

        - Non-retryable exceptions (`InputValidationError`, `RateLimitExceededError`,
          `PermissionError`) are raised immediately on the first failure without retrying.
        - Retryable exceptions are retried up to `max_retries` times; if still failing,
          raises `RetryLimitExceededError`.
        """
        limit = self.resolve_max_retries(branch_id=branch_id)
        attempts = 0
        last_err: Optional[BaseException] = None

        while attempts <= limit:
            attempts += 1
            try:
                val = operation()
                return RetryOutcome(
                    result=val,
                    attempts_made=attempts,
                    retries_used=attempts - 1,
                )
            except self._non_retryable:
                # Fail fast: bad inputs and security/rate-limit errors must never be retried
                raise
            except Exception as exc:
                last_err = exc
                if attempts > limit:
                    break
                if self._base_backoff > 0:
                    self._sleep_fn(self._base_backoff * attempts)

        raise RetryLimitExceededError(
            f"Operation '{operation_name}' failed after {attempts} attempts "
            f"(max_retries={limit} exceeded). Last error: {last_err}",
            attempts_made=attempts,
            max_retries=limit,
            last_error=last_err,
        )

    def check_hitl_rejection_retry(
        self,
        current_retry_count: int,
        *,
        branch_id: int = 0,
    ) -> int:
        """Validates and increments `retry_count` when a human reviewer rejects a draft (RS-7 / RS-8).

        Raises `RetryLimitExceededError` once `next_retry_count > max_retries` so the
        workflow stops and escalates to a named operator.
        """
        valid_count = InputValidator.validate_non_negative_int(
            current_retry_count, "retry_count"
        )
        limit = self.resolve_max_retries(branch_id=branch_id)
        next_count = valid_count + 1
        if next_count > limit:
            raise RetryLimitExceededError(
                f"HITL rejection retry limit reached ({next_count} > max_retries={limit}). "
                "Escalating to operator.",
                attempts_made=next_count,
                max_retries=limit,
            )
        return next_count
