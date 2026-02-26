"""Unit tests for assessment_engine.py — all 6 public functions."""

from datetime import datetime, timedelta

from assessment_engine import (
    _dominant_error_type,
    check_break_needed,
    compute_mastery,
    compute_recommendation,
    compute_trajectory,
    detect_productive_failure,
)
from learner_model import Attempt, BreakState, TopicGraph, TopicState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_attempt(is_correct: bool, error_type: str | None = None) -> Attempt:
    return Attempt(
        question_id="q",
        learner_answer="a",
        correct_answer="a",
        is_correct=is_correct,
        error_type=error_type,
    )


def make_attempts(specs: list[tuple[bool, str | None]]) -> list[Attempt]:
    """specs is a list of (is_correct, error_type) tuples."""
    return [make_attempt(c, e) for c, e in specs]


# ---------------------------------------------------------------------------
# compute_trajectory
# ---------------------------------------------------------------------------

class TestComputeTrajectory:
    def test_insufficient_data(self):
        attempts = make_attempts([(True, None), (True, None)])
        assert compute_trajectory(attempts) == "unknown"

    def test_empty_list(self):
        assert compute_trajectory([]) == "unknown"

    def test_improving(self):
        """First half errors, second half correct → improving."""
        attempts = make_attempts([
            (False, "structural"),
            (False, "structural"),
            (False, "conceptual"),
            (True, None),
            (True, None),
            (True, None),
        ])
        assert compute_trajectory(attempts) == "improving"

    def test_declining(self):
        """First half correct, second half errors → declining."""
        attempts = make_attempts([
            (True, None),
            (True, None),
            (True, None),
            (False, "structural"),
            (False, "conceptual"),
            (False, "conceptual"),
        ])
        assert compute_trajectory(attempts) == "declining"

    def test_flat(self):
        """All correct → flat (no severity change)."""
        attempts = make_attempts([(True, None)] * 5)
        assert compute_trajectory(attempts) == "flat"

    def test_exactly_three_attempts(self):
        """Boundary: exactly 3 attempts should not return unknown."""
        attempts = make_attempts([
            (False, "structural"),
            (True, None),
            (True, None),
        ])
        result = compute_trajectory(attempts)
        assert result in ("improving", "flat", "declining")

    def test_window_limits(self):
        """Only the last `window` attempts are considered."""
        old = make_attempts([(False, "conceptual")] * 10)
        recent = make_attempts([(True, None)] * 5)
        # With window=5, only the recent all-correct attempts are seen
        assert compute_trajectory(old + recent, window=5) == "flat"


# ---------------------------------------------------------------------------
# compute_mastery
# ---------------------------------------------------------------------------

class TestComputeMastery:
    def test_empty_history(self):
        ts = TopicState()
        assert compute_mastery(ts) == 0.0

    def test_single_correct(self):
        ts = TopicState(attempt_history=make_attempts([(True, None)]))
        mastery = compute_mastery(ts)
        # confidence = 1/6 ≈ 0.167, raw = 1.0 → ~0.167
        assert 0.1 < mastery < 0.25

    def test_ten_correct(self):
        ts = TopicState(attempt_history=make_attempts([(True, None)] * 10))
        mastery = compute_mastery(ts)
        # confidence = 10/15 ≈ 0.667, raw = 1.0
        assert 0.6 < mastery < 0.75

    def test_window_caps_at_ten(self):
        """20 correct should give same mastery as 10 (window=10)."""
        ts10 = TopicState(attempt_history=make_attempts([(True, None)] * 10))
        ts20 = TopicState(attempt_history=make_attempts([(True, None)] * 20))
        assert abs(compute_mastery(ts10) - compute_mastery(ts20)) < 0.01

    def test_all_conceptual_errors(self):
        ts = TopicState(
            attempt_history=make_attempts([(False, "conceptual")] * 5)
        )
        assert compute_mastery(ts) == 0.0

    def test_computational_errors_partial_credit(self):
        ts = TopicState(
            attempt_history=make_attempts([(False, "computational")] * 5)
        )
        mastery = compute_mastery(ts)
        # computational = 0.5 score, so mastery > 0 but < all-correct
        assert 0.0 < mastery < compute_mastery(
            TopicState(attempt_history=make_attempts([(True, None)] * 5))
        )

    def test_recency_weighting(self):
        """Late correct answers should produce higher mastery than early ones."""
        early_correct = make_attempts([
            (True, None), (True, None),
            (False, "structural"), (False, "structural"), (False, "structural"),
        ])
        late_correct = make_attempts([
            (False, "structural"), (False, "structural"), (False, "structural"),
            (True, None), (True, None),
        ])
        ts_early = TopicState(attempt_history=early_correct)
        ts_late = TopicState(attempt_history=late_correct)
        assert compute_mastery(ts_late) > compute_mastery(ts_early)

    def test_confidence_scales_with_count(self):
        """More attempts → higher confidence → higher mastery for same raw score."""
        ts3 = TopicState(attempt_history=make_attempts([(True, None)] * 3))
        ts8 = TopicState(attempt_history=make_attempts([(True, None)] * 8))
        assert compute_mastery(ts8) > compute_mastery(ts3)


# ---------------------------------------------------------------------------
# _dominant_error_type
# ---------------------------------------------------------------------------

class TestDominantErrorType:
    def test_no_errors(self):
        attempts = make_attempts([(True, None)] * 3)
        assert _dominant_error_type(attempts) is None

    def test_empty_list(self):
        assert _dominant_error_type([]) is None

    def test_single_error_type(self):
        attempts = make_attempts([
            (False, "computational"),
            (False, "computational"),
        ])
        assert _dominant_error_type(attempts) == "computational"

    def test_mixed_errors(self):
        attempts = make_attempts([
            (False, "computational"),
            (False, "structural"),
            (False, "structural"),
        ])
        assert _dominant_error_type(attempts) == "structural"

    def test_none_error_type_defaults_to_structural(self):
        """A wrong answer with error_type=None should be counted as structural."""
        attempts = make_attempts([(False, None)])
        assert _dominant_error_type(attempts) == "structural"


# ---------------------------------------------------------------------------
# detect_productive_failure
# ---------------------------------------------------------------------------

class TestDetectProductiveFailure:
    def test_computational_wrong(self):
        a = make_attempt(False, "computational")
        assert detect_productive_failure(a) is True

    def test_structural_wrong(self):
        a = make_attempt(False, "structural")
        assert detect_productive_failure(a) is False

    def test_conceptual_wrong(self):
        a = make_attempt(False, "conceptual")
        assert detect_productive_failure(a) is False

    def test_correct_answer(self):
        a = make_attempt(True, None)
        assert detect_productive_failure(a) is False


# ---------------------------------------------------------------------------
# check_break_needed
# ---------------------------------------------------------------------------

class TestCheckBreakNeeded:
    def test_default_state(self):
        bs = BreakState()
        result = check_break_needed(bs, "flat")
        assert result["needed"] is False

    def test_declining_plus_three_errors(self):
        bs = BreakState(consecutive_errors=3)
        result = check_break_needed(bs, "declining")
        assert result["needed"] is True
        assert result["urgency"] == "high"

    def test_five_consecutive_errors(self):
        bs = BreakState(consecutive_errors=5)
        result = check_break_needed(bs, "flat")
        assert result["needed"] is True
        assert result["urgency"] == "high"

    def test_long_session_no_break(self):
        start = (datetime.now() - timedelta(minutes=50)).isoformat()
        bs = BreakState(session_start_time=start)
        result = check_break_needed(bs, "flat")
        assert result["needed"] is True
        assert result["urgency"] == "medium"

    def test_long_since_last_break(self):
        start = (datetime.now() - timedelta(minutes=120)).isoformat()
        last_break = (datetime.now() - timedelta(minutes=50)).isoformat()
        bs = BreakState(session_start_time=start, last_break_taken=last_break)
        result = check_break_needed(bs, "flat")
        assert result["needed"] is True
        assert result["urgency"] == "medium"

    def test_escalating_severity(self):
        bs = BreakState(error_severity_trend=[1, 2, 3])
        result = check_break_needed(bs, "flat")
        assert result["needed"] is True
        assert result["urgency"] == "medium"

    def test_decreasing_severity(self):
        bs = BreakState(error_severity_trend=[3, 2, 1])
        result = check_break_needed(bs, "flat")
        assert result["needed"] is False

    def test_within_cooldown(self):
        """Even with triggers, should not fire within cooldown."""
        recent = (datetime.now() - timedelta(minutes=5)).isoformat()
        bs = BreakState(
            consecutive_errors=5,
            last_break_suggestion=recent,
            break_cooldown_minutes=10,
        )
        result = check_break_needed(bs, "declining")
        assert result["needed"] is False

    def test_past_cooldown(self):
        """Past cooldown, triggers should fire."""
        old = (datetime.now() - timedelta(minutes=15)).isoformat()
        bs = BreakState(
            consecutive_errors=5,
            last_break_suggestion=old,
            break_cooldown_minutes=10,
        )
        result = check_break_needed(bs, "flat")
        assert result["needed"] is True


# ---------------------------------------------------------------------------
# compute_recommendation
# ---------------------------------------------------------------------------

class TestComputeRecommendation:
    def test_high_mastery_no_errors(self):
        r = compute_recommendation("flat", None, 0.90, None, "math")
        assert r["action"] == "keep_grinding"

    def test_computational_flat(self):
        r = compute_recommendation("flat", "computational", 0.5, None, "math")
        assert r["action"] == "brief_tip"

    def test_computational_improving(self):
        r = compute_recommendation("improving", "computational", 0.5, None, "math")
        assert r["action"] == "keep_grinding"

    def test_structural_flat(self):
        r = compute_recommendation("flat", "structural", 0.5, None, "math")
        assert r["action"] == "targeted_instruction"

    def test_declining_with_prereqs(self):
        tg = TopicGraph(prerequisites={"math": ["arithmetic"]})
        r = compute_recommendation("declining", "structural", 0.3, tg, "math")
        assert r["action"] == "go_back"
        assert r["prerequisite_topic"] == "arithmetic"

    def test_declining_no_prereqs(self):
        r = compute_recommendation("declining", "structural", 0.3, None, "math")
        assert r["action"] == "take_break"

    def test_break_state_triggers_break(self):
        bs = BreakState(consecutive_errors=5)
        r = compute_recommendation("flat", "structural", 0.3, None, "math", break_state=bs)
        assert r["action"] == "take_break"

    def test_post_break_warmup(self):
        bs = BreakState(post_break_warmup=True)
        r = compute_recommendation("flat", None, 0.5, None, "math", break_state=bs)
        assert r["action"] == "warmup"

    def test_conceptual_flat_with_prereqs(self):
        tg = TopicGraph(prerequisites={"math": ["basics"]})
        r = compute_recommendation("flat", "conceptual", 0.3, tg, "math")
        assert r["action"] == "go_back"
        assert r["prerequisite_topic"] == "basics"

    def test_conceptual_improving(self):
        r = compute_recommendation("improving", "conceptual", 0.3, None, "math")
        assert r["action"] == "targeted_instruction"

    def test_no_dominant_error_low_mastery(self):
        r = compute_recommendation("flat", None, 0.5, None, "math")
        assert r["action"] == "keep_grinding"
