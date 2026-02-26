"""
Assessment engine — pure logic, no I/O.
Trajectory computation, mastery scoring, recommendation engine, break detection.
"""

from datetime import datetime, timedelta
from typing import Optional

from learner_model import Attempt, BreakState, TopicGraph, TopicState

# Severity scores for error types (lower = better)
ERROR_SEVERITY = {
    None: 0,        # correct answer
    "correct": 0,
    "computational": 1,
    "structural": 2,
    "conceptual": 3,
}

BLOOM_LEVELS = ["remember", "understand", "apply", "analyze", "evaluate", "create"]


def compute_trajectory(attempts: list[Attempt], window: int = 5) -> str:
    """
    Compare average error severity of first half vs second half of recent window.
    Delta > 0.3 improving, < -0.3 declining, else flat.
    Severity goes DOWN when improving (fewer/lighter errors).
    """
    if len(attempts) < 3:
        return "unknown"

    recent = attempts[-window:]
    if len(recent) < 3:
        return "unknown"

    mid = len(recent) // 2
    first_half = recent[:mid]
    second_half = recent[mid:]

    def avg_severity(atts: list[Attempt]) -> float:
        scores = []
        for a in atts:
            if a.is_correct:
                scores.append(0)
            else:
                scores.append(ERROR_SEVERITY.get(a.error_type, 2))
        return sum(scores) / len(scores) if scores else 0.0

    first_avg = avg_severity(first_half)
    second_avg = avg_severity(second_half)
    delta = first_avg - second_avg  # positive = improving (severity went down)

    if delta > 0.3:
        return "improving"
    elif delta < -0.3:
        return "declining"
    return "flat"


def compute_mastery(topic_state: TopicState, window: int = 10) -> float:
    """
    Weighted recent accuracy with recency weighting and confidence scaling.
    correct=1.0, computational=0.5, structural=0.25, conceptual=0.0

    A confidence factor scales the raw score so that fewer attempts yield
    a lower mastery.  With 1 attempt you can reach at most ~40%; the score
    asymptotically approaches the raw accuracy as attempts grow toward the
    window size.
    """
    attempts = topic_state.attempt_history
    if not attempts:
        return 0.0

    recent = attempts[-window:]
    accuracy_scores = {
        "correct": 1.0,
        "computational": 0.5,
        "structural": 0.25,
        "conceptual": 0.0,
    }

    weighted_sum = 0.0
    weight_total = 0.0

    for i, attempt in enumerate(recent):
        recency_weight = (i + 1) / len(recent)  # linear: newer = higher weight
        if attempt.is_correct:
            score = 1.0
        else:
            score = accuracy_scores.get(attempt.error_type, 0.25)
        weighted_sum += score * recency_weight
        weight_total += recency_weight

    raw = weighted_sum / weight_total if weight_total > 0 else 0.0

    # Confidence: n / (n + k) where k controls how many attempts are needed
    # to reach full confidence.  k=5 means 5 attempts → 50% confidence.
    n = len(recent)
    confidence = n / (n + 5)

    return raw * confidence


def _dominant_error_type(attempts: list[Attempt], window: int = 5) -> Optional[str]:
    """Find the most common error type in recent wrong answers."""
    recent_errors = [a for a in attempts[-window:] if not a.is_correct]
    if not recent_errors:
        return None

    counts: dict[str, int] = {}
    for a in recent_errors:
        et = a.error_type or "structural"
        counts[et] = counts.get(et, 0) + 1

    return max(counts, key=counts.get)


def detect_productive_failure(attempt: Attempt) -> bool:
    """
    Productive failure: wrong answer but computational error type
    (they knew the method, just made a calculation mistake).
    """
    return not attempt.is_correct and attempt.error_type == "computational"


def check_break_needed(break_state: BreakState, trajectory: str) -> dict:
    """
    Determine if a break should be suggested.

    Triggers:
    1. Declining trajectory + 3+ consecutive errors
    2. 5+ consecutive errors regardless of trajectory
    3. Session running 45+ minutes without a break
    4. Error severity escalating (3+ increasing severity scores)

    Returns dict with 'needed' bool, 'reason' string, 'urgency' (low/medium/high).
    Respects cooldown: won't suggest another break within cooldown_minutes of last suggestion.
    """
    result = {"needed": False, "reason": "", "urgency": "low"}

    # Respect cooldown
    if break_state.last_break_suggestion:
        last_suggestion = datetime.fromisoformat(break_state.last_break_suggestion)
        cooldown = timedelta(minutes=break_state.break_cooldown_minutes)
        if datetime.now() - last_suggestion < cooldown:
            return result

    # Trigger 1: Declining + consecutive errors
    if trajectory == "declining" and break_state.consecutive_errors >= 3:
        result["needed"] = True
        result["reason"] = (
            f"Your trajectory is declining and you've had {break_state.consecutive_errors} "
            "consecutive errors. A short break can help reset your focus."
        )
        result["urgency"] = "high"
        return result

    # Trigger 2: Many consecutive errors
    if break_state.consecutive_errors >= 5:
        result["needed"] = True
        result["reason"] = (
            f"You've had {break_state.consecutive_errors} consecutive errors. "
            "Sometimes stepping away for a few minutes helps you see things fresh."
        )
        result["urgency"] = "high"
        return result

    # Trigger 3: Long session
    if break_state.session_start_time:
        session_start = datetime.fromisoformat(break_state.session_start_time)
        elapsed = datetime.now() - session_start
        # Account for breaks already taken
        effective_minutes = elapsed.total_seconds() / 60
        last_break = break_state.last_break_taken
        if last_break:
            since_break = (datetime.now() - datetime.fromisoformat(last_break)).total_seconds() / 60
            effective_minutes = since_break

        if effective_minutes >= 45:
            result["needed"] = True
            result["reason"] = (
                f"You've been working for about {int(effective_minutes)} minutes "
                "without a break. A 5-10 minute break can improve retention."
            )
            result["urgency"] = "medium"
            return result

    # Trigger 4: Error severity escalating
    trend = break_state.error_severity_trend
    if len(trend) >= 3:
        last_three = trend[-3:]
        if last_three[0] < last_three[1] < last_three[2] and last_three[2] >= 2:
            result["needed"] = True
            result["reason"] = (
                "Your errors are getting more fundamental over time. "
                "A break might help you approach the material with fresh eyes."
            )
            result["urgency"] = "medium"
            return result

    return result


def compute_recommendation(
    trajectory: str,
    dominant_error: Optional[str],
    mastery: float,
    topic_graph: Optional[TopicGraph],
    topic: str,
    break_state: Optional[BreakState] = None,
) -> dict:
    """
    Decision matrix for next action.
    Returns dict with 'action', 'detail', and optionally 'prerequisite_topic'.
    """
    # Check break first
    if break_state:
        break_check = check_break_needed(break_state, trajectory)
        if break_check["needed"]:
            return {
                "action": "take_break",
                "detail": break_check["reason"],
                "urgency": break_check["urgency"],
                "post_break_warmup": True,
            }

    # Post-break warmup: give an easier question
    if break_state and break_state.post_break_warmup:
        return {
            "action": "warmup",
            "detail": (
                "Welcome back! Let's start with a gentler question to ease back in."
            ),
        }

    # All correct and mastery >= 0.85: advance
    if dominant_error is None and mastery >= 0.85:
        return {
            "action": "keep_grinding",
            "detail": (
                "Mastery is strong. Increase difficulty — move up Bloom's taxonomy "
                "or introduce edge cases."
            ),
        }

    # Declining trajectory
    if trajectory == "declining":
        if topic_graph and topic in topic_graph.prerequisites:
            prereqs = topic_graph.prerequisites[topic]
            if prereqs:
                return {
                    "action": "go_back",
                    "detail": "Trajectory declining. Revisit prerequisite material.",
                    "prerequisite_topic": prereqs[0],
                }
        return {
            "action": "take_break",
            "detail": (
                "Trajectory declining and no prerequisite to fall back to. "
                "Suggest a short break before trying a different angle."
            ),
            "urgency": "medium",
            "post_break_warmup": True,
        }

    # Conceptual errors
    if dominant_error == "conceptual":
        if trajectory == "improving":
            return {
                "action": "targeted_instruction",
                "detail": (
                    "Conceptual gaps but improving. Provide a clear explanation "
                    "of the underlying concept with a worked example."
                ),
            }
        # flat or unknown
        if topic_graph and topic in topic_graph.prerequisites:
            prereqs = topic_graph.prerequisites[topic]
            if prereqs:
                return {
                    "action": "go_back",
                    "detail": "Persistent conceptual errors. Go back to prerequisites.",
                    "prerequisite_topic": prereqs[0],
                }
        return {
            "action": "targeted_instruction",
            "detail": (
                "Conceptual errors without prerequisite to revisit. "
                "Give a thorough explanation with multiple examples."
            ),
        }

    # Structural errors
    if dominant_error == "structural":
        if trajectory == "improving":
            return {
                "action": "keep_grinding",
                "detail": (
                    "Structural errors but improving. Continue with hints "
                    "about the correct method/approach."
                ),
            }
        return {
            "action": "targeted_instruction",
            "detail": (
                "Structural errors not improving. Teach the correct method "
                "step-by-step with a worked example."
            ),
        }

    # Computational errors
    if dominant_error == "computational":
        if trajectory == "improving":
            return {
                "action": "keep_grinding",
                "detail": "Computational errors but improving. Keep practicing.",
            }
        return {
            "action": "brief_tip",
            "detail": (
                "Computation errors. Give a quick tip about careful calculation "
                "or common pitfalls, then continue."
            ),
        }

    # Default
    return {
        "action": "keep_grinding",
        "detail": "Continue with appropriately leveled questions.",
    }
