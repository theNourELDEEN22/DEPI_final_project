"""Unit tests for Task A7: Input validation, rate limit and retry middleware (FD-4).

Verifies the Done Condition: 'Bad input rejected; retries are bounded',
plus rate limiting and fail-fast behavior on validation errors.
"""
import unittest
from decimal import Decimal

from foundation.config_service import ConfigService, InMemoryConfigBackend
from foundation.validation_retry_middleware import (
    BoundedRetryPolicy,
    InputValidationError,
    InputValidator,
    RateLimitExceededError,
    RetryLimitExceededError,
    SlidingWindowRateLimiter,
)


class TestValidationRateLimitRetryA7(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ConfigService(backend=InMemoryConfigBackend())

    # -------------------------------------------------------------------------
    # 1. Input Validation Tests ("Bad input rejected")
    # -------------------------------------------------------------------------
    def test_bad_quantity_inputs_are_rejected(self) -> None:
        """Task A7 Done Condition (Part 1): Bad input rejected (0, negative, float, bool, str)."""
        for bad_qty in [0, -1, -50, 3.5, 10.0, True, False, "15", None, []]:
            with self.subTest(bad_qty=bad_qty):
                with self.assertRaises(InputValidationError):
                    InputValidator.validate_positive_int(bad_qty, "requested_quantity")

        # Valid positive integer passes
        self.assertEqual(InputValidator.validate_positive_int(24, "requested_quantity"), 24)

    def test_bad_restock_order_payloads_are_rejected(self) -> None:
        """Full restock draft validation blocks bad quantities, branch IDs, or demand levels."""
        valid_payload = {
            "branch_id": 1,
            "product_id": 1,
            "supplier_id": 1,
            "requested_quantity": 50,
            "run_id": "run-maadi-2026-10-29",
            "demand_level": "High",
        }
        cleaned = InputValidator.validate_restock_order_input(valid_payload)
        self.assertEqual(cleaned["requested_quantity"], 50)

        # Zero quantity rejected
        with self.assertRaises(InputValidationError) as ctx:
            InputValidator.validate_restock_order_input({**valid_payload, "requested_quantity": 0})
        self.assertEqual(ctx.exception.field_name, "requested_quantity")

        # Unknown demand_level rejected
        with self.assertRaises(InputValidationError) as ctx:
            InputValidator.validate_restock_order_input({**valid_payload, "demand_level": "Extreme"})
        self.assertEqual(ctx.exception.field_name, "demand_level")

        # Empty run_id rejected
        with self.assertRaises(InputValidationError) as ctx:
            InputValidator.validate_restock_order_input({**valid_payload, "run_id": "   "})
        self.assertEqual(ctx.exception.field_name, "run_id")

    def test_decimal_money_and_date_order_validation(self) -> None:
        """Rejects floats/negatives for money and inverted production/expiration dates."""
        with self.assertRaises(InputValidationError):
            InputValidator.validate_decimal_money(29.25, "unit_price")  # float rejected
        with self.assertRaises(InputValidationError):
            InputValidator.validate_decimal_money("-5.00", "unit_price")  # negative rejected

        self.assertEqual(
            InputValidator.validate_decimal_money("29.25", "unit_price"),
            Decimal("29.25"),
        )

        # Expiration earlier than production is rejected
        with self.assertRaises(InputValidationError):
            InputValidator.validate_date_order("2025-05-10", "2025-05-01")

        prod, exp = InputValidator.validate_date_order("2025-01-01", "2025-01-06")
        self.assertEqual(str(prod), "2025-01-01")
        self.assertEqual(str(exp), "2025-01-06")

    # -------------------------------------------------------------------------
    # 2. Rate Limiter Tests
    # -------------------------------------------------------------------------
    def test_rate_limiter_bounds_burst_requests_and_recovers_after_window(self) -> None:
        """SlidingWindowRateLimiter blocks requests above max_requests within window."""
        now = [100.0]
        limiter = SlidingWindowRateLimiter(
            max_requests=3,
            window_seconds=10.0,
            clock=lambda: now[0],
        )

        self.assertEqual(limiter.check_and_record("branch:1:restock"), 2)
        self.assertEqual(limiter.check_and_record("branch:1:restock"), 1)
        self.assertEqual(limiter.check_and_record("branch:1:restock"), 0)

        # 4th request within the same 10-second window is rejected
        with self.assertRaises(RateLimitExceededError):
            limiter.check_and_record("branch:1:restock")

        # Advance clock past the 10-second window -> allowed again
        now[0] = 111.0
        self.assertEqual(limiter.check_and_record("branch:1:restock"), 2)

    # -------------------------------------------------------------------------
    # 3. Bounded Retry Tests ("retries are bounded")
    # -------------------------------------------------------------------------
    def test_retries_are_bounded_on_persistent_failure(self) -> None:
        """Task A7 Done Condition (Part 2): Retries are bounded by max_retries (default 3)."""
        policy = BoundedRetryPolicy(config_service=self.config)  # max_retries = 3 from ConfigService
        call_counter = {"count": 0}

        def always_fails() -> str:
            call_counter["count"] += 1
            raise RuntimeError("Transient database timeout")

        with self.assertRaises(RetryLimitExceededError) as ctx:
            policy.execute(always_fails, operation_name="query_inventory_db")

        # 1 initial attempt + 3 retries = 4 total attempts maximum
        self.assertEqual(call_counter["count"], 4)
        self.assertEqual(ctx.exception.attempts_made, 4)
        self.assertEqual(ctx.exception.max_retries, 3)

    def test_retry_succeeds_before_limit_and_validation_error_never_retries(self) -> None:
        """Succeeds if transient failure recovers within bound; never retries InputValidationError."""
        policy = BoundedRetryPolicy(max_retries=3)
        state = {"calls": 0}

        def succeeds_on_third_try() -> str:
            state["calls"] += 1
            if state["calls"] < 3:
                raise ConnectionError("Temporary network glitch")
            return "OK"

        outcome = policy.execute(succeeds_on_third_try)
        self.assertEqual(outcome.result, "OK")
        self.assertEqual(outcome.attempts_made, 3)
        self.assertEqual(outcome.retries_used, 2)

        # Non-retryable InputValidationError fails immediately on attempt 1
        bad_calls = {"calls": 0}

        def bad_input_op() -> None:
            bad_calls["calls"] += 1
            InputValidator.validate_positive_int(-10, "requested_quantity")

        with self.assertRaises(InputValidationError):
            policy.execute(bad_input_op)
        self.assertEqual(bad_calls["calls"], 1)

    def test_hitl_rejection_loop_is_bounded(self) -> None:
        """Human review rejection loop increments retry_count up to max_retries (3) then halts."""
        policy = BoundedRetryPolicy(config_service=self.config)  # max_retries = 3
        self.assertEqual(policy.check_hitl_rejection_retry(0), 1)
        self.assertEqual(policy.check_hitl_rejection_retry(1), 2)
        self.assertEqual(policy.check_hitl_rejection_retry(2), 3)

        # 4th rejection exceeds max_retries=3 -> raises RetryLimitExceededError for operator escalation
        with self.assertRaises(RetryLimitExceededError):
            policy.check_hitl_rejection_retry(3)


if __name__ == "__main__":
    unittest.main()
