"""
Adaptive Tutoring MCP Server.
Exposes tools for session management, attempt recording, assessment, and break tracking.
"""

import os
import sys

# Ensure sibling modules are importable
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime
from typing import Optional

from fastmcp import FastMCP

from assessment_engine import (
    check_break_needed,
    compute_mastery,
    compute_recommendation,
    compute_trajectory,
    detect_productive_failure,
    ERROR_SEVERITY,
    _dominant_error_type,
)
from learner_model import (
    Attempt,
    BreakState,
    LearnerProfile,
    Misconception,
    SessionSummary,
    TopicGraph,
    TopicState,
    load_learner,
    rebuild_unified_graph,
    save_learner,
)

mcp = FastMCP("AdaptiveTutor")


def _normalize_topic(topic: str) -> str:
    """Normalize topic names so 'Markov Process', 'markov process', and
    'markov_process' all resolve to the same key."""
    return topic.strip().lower().replace(" ", "_").replace("-", "_")


@mcp.tool()
def start_session(learner_id: str, topic: str) -> dict:
    """
    Start or resume a tutoring session.
    Loads the learner profile, initializes the topic if new,
    and returns current state including whether a topic graph is needed.
    """
    topic = _normalize_topic(topic)
    profile = load_learner(learner_id)
    profile.session_count += 1

    if topic not in profile.topics:
        profile.topics[topic] = TopicState()

    ts = profile.topics[topic]

    # Initialize break state for this session
    ts.break_state.session_start_time = datetime.now().isoformat()
    ts.break_state.consecutive_errors = 0
    ts.break_state.post_break_warmup = False
    ts.break_state.error_severity_trend = []

    needs_topic_graph = topic not in profile.topic_graphs

    # Create session summary
    summary = SessionSummary(
        session_number=profile.session_count,
        topic=topic,
        start_time=datetime.now().isoformat(),
        mastery_start=ts.mastery_level,
    )
    profile.session_history.append(summary)

    save_learner(profile)

    return {
        "status": "session_started",
        "learner_id": learner_id,
        "topic": topic,
        "session_number": profile.session_count,
        "mastery_level": ts.mastery_level,
        "trajectory": ts.trajectory,
        "total_attempts": len(ts.attempt_history),
        "needs_topic_graph": needs_topic_graph,
        "zpd": ts.zpd.model_dump(),
        "unresolved_misconceptions": [
            m.description for m in ts.misconceptions if not m.resolved
        ],
    }


@mcp.tool()
def record_attempt(
    learner_id: str,
    topic: str,
    question_id: str,
    learner_answer: str,
    correct_answer: str,
    is_correct: bool,
    error_type: Optional[str] = None,
    error_step: Optional[str] = None,
    bloom_level: Optional[str] = None,
) -> dict:
    """
    Record a learner's attempt at a question.
    Recomputes mastery and trajectory after recording.
    Updates break state (consecutive errors, severity trend).
    Returns updated assessment state.
    """
    topic = _normalize_topic(topic)
    profile = load_learner(learner_id)

    if topic not in profile.topics:
        profile.topics[topic] = TopicState()

    ts = profile.topics[topic]
    bs = ts.break_state

    attempt = Attempt(
        question_id=question_id,
        learner_answer=learner_answer,
        correct_answer=correct_answer,
        is_correct=is_correct,
        error_type=error_type,
        error_step=error_step,
        bloom_level=bloom_level,
    )

    ts.attempt_history.append(attempt)

    # Update break state
    if is_correct:
        bs.consecutive_errors = 0
        bs.post_break_warmup = False  # warmup complete on correct answer
    else:
        bs.consecutive_errors += 1
        severity = ERROR_SEVERITY.get(error_type, 2)
        bs.error_severity_trend.append(severity)
        # Keep only last 10 severity scores
        if len(bs.error_severity_trend) > 10:
            bs.error_severity_trend = bs.error_severity_trend[-10:]

    # Detect productive failure
    if detect_productive_failure(attempt):
        ts.productive_failures += 1

    # Recompute
    ts.trajectory = compute_trajectory(ts.attempt_history)
    ts.mastery_level = compute_mastery(ts)

    # Update session summary
    if profile.session_history:
        current = profile.session_history[-1]
        current.attempts_count += 1
        if is_correct:
            current.correct_count += 1
        current.mastery_end = ts.mastery_level

    save_learner(profile)

    return {
        "recorded": True,
        "mastery_level": ts.mastery_level,
        "trajectory": ts.trajectory,
        "productive_failure": detect_productive_failure(attempt),
        "consecutive_errors": bs.consecutive_errors,
        "total_attempts": len(ts.attempt_history),
    }


@mcp.tool()
def get_assessment(learner_id: str, topic: str) -> dict:
    """
    Run the decision engine and return a recommendation for the next action.
    Includes break detection in the assessment.
    """
    topic = _normalize_topic(topic)
    profile = load_learner(learner_id)

    if topic not in profile.topics:
        return {"error": f"No data for topic '{topic}'. Start a session first."}

    ts = profile.topics[topic]
    dominant_error = _dominant_error_type(ts.attempt_history)
    topic_graph = profile.topic_graphs.get(topic)

    recommendation = compute_recommendation(
        trajectory=ts.trajectory,
        dominant_error=dominant_error,
        mastery=ts.mastery_level,
        topic_graph=topic_graph,
        topic=topic,
        break_state=ts.break_state,
    )

    return {
        "mastery_level": ts.mastery_level,
        "trajectory": ts.trajectory,
        "dominant_error_type": dominant_error,
        "recommendation": recommendation,
        "zpd": ts.zpd.model_dump(),
        "productive_failures": ts.productive_failures,
        "consecutive_errors": ts.break_state.consecutive_errors,
        "unresolved_misconceptions": [
            m.description for m in ts.misconceptions if not m.resolved
        ],
    }


@mcp.tool()
def get_learner_profile(learner_id: str) -> dict:
    """Return the full learner profile as a dictionary."""
    profile = load_learner(learner_id)
    return profile.model_dump()


@mcp.tool()
def update_topic_mastery(learner_id: str, topic: str, mastery_level: float) -> dict:
    """Manually override the mastery level for a topic (0.0 to 1.0)."""
    topic = _normalize_topic(topic)
    profile = load_learner(learner_id)

    if topic not in profile.topics:
        profile.topics[topic] = TopicState()

    profile.topics[topic].mastery_level = max(0.0, min(1.0, mastery_level))
    save_learner(profile)

    return {
        "updated": True,
        "topic": topic,
        "mastery_level": profile.topics[topic].mastery_level,
    }


@mcp.tool()
def record_misconception(learner_id: str, topic: str, description: str) -> dict:
    """
    Log a misconception or increment its count if already observed.
    """
    topic = _normalize_topic(topic)
    profile = load_learner(learner_id)

    if topic not in profile.topics:
        profile.topics[topic] = TopicState()

    ts = profile.topics[topic]

    # Check if this misconception already exists
    for m in ts.misconceptions:
        if m.description.lower() == description.lower():
            m.times_observed += 1
            m.last_seen = datetime.now().isoformat()
            if m.resolved:
                m.resolved = False  # re-opened
            save_learner(profile)
            return {
                "recorded": True,
                "new": False,
                "description": m.description,
                "times_observed": m.times_observed,
            }

    # New misconception
    misconception = Misconception(description=description)
    ts.misconceptions.append(misconception)
    save_learner(profile)

    return {
        "recorded": True,
        "new": True,
        "description": description,
        "times_observed": 1,
    }


@mcp.tool()
def resolve_misconception(learner_id: str, topic: str, description: str) -> dict:
    """
    Mark a misconception as resolved after the learner demonstrates understanding.
    Finds the misconception by case-insensitive description match and sets resolved=True.
    """
    topic = _normalize_topic(topic)
    profile = load_learner(learner_id)

    if topic not in profile.topics:
        return {"resolved": False, "error": f"No data for topic '{topic}'."}

    ts = profile.topics[topic]

    for m in ts.misconceptions:
        if m.description.lower() == description.lower():
            m.resolved = True
            m.last_seen = datetime.now().isoformat()
            save_learner(profile)
            return {
                "resolved": True,
                "description": m.description,
                "times_observed": m.times_observed,
            }

    return {
        "resolved": False,
        "error": f"Misconception not found: '{description}'",
    }


@mcp.tool()
def store_topic_graph(
    learner_id: str, topic: str, prerequisites: dict[str, list[str]]
) -> dict:
    """
    Save an AI-generated prerequisite graph for a topic.
    prerequisites is a dict mapping each subtopic to its prerequisite subtopics.
    Example: {"integrals": ["derivatives", "limits"], "derivatives": ["limits"]}
    """
    topic = _normalize_topic(topic)
    profile = load_learner(learner_id)
    profile.topic_graphs[topic] = TopicGraph(prerequisites=prerequisites)
    rebuild_unified_graph(profile)
    save_learner(profile)

    return {
        "stored": True,
        "topic": topic,
        "prerequisite_count": len(prerequisites),
    }


@mcp.tool()
def add_topics(learner_id: str, topics: list[str]) -> dict:
    """
    Add one or more topics to the learner's learning list without starting a session.
    Creates topic entries with default state. Returns which topics were added
    and which need a prerequisite graph generated via store_topic_graph.
    """
    profile = load_learner(learner_id)
    added = []
    already_existed = []
    needs_topic_graph = []

    for raw_topic in topics:
        topic = _normalize_topic(raw_topic)
        if topic in profile.topics:
            already_existed.append(topic)
        else:
            profile.topics[topic] = TopicState()
            added.append(topic)
            if topic not in profile.topic_graphs:
                needs_topic_graph.append(topic)

    save_learner(profile)

    return {
        "added": added,
        "already_existed": already_existed,
        "needs_topic_graph": needs_topic_graph,
    }


@mcp.tool()
def delete_topic(learner_id: str, topic: str) -> dict:
    """Delete a topic and all its data from the learner's profile.
    Removes topic state and topic graph. Returns remaining topics."""
    topic = _normalize_topic(topic)
    profile = load_learner(learner_id)
    if topic not in profile.topics:
        return {"deleted": False, "error": f"Topic '{topic}' not found.",
                "available_topics": list(profile.topics.keys())}
    del profile.topics[topic]
    if topic in profile.topic_graphs:
        del profile.topic_graphs[topic]
    rebuild_unified_graph(profile)
    save_learner(profile)
    return {"deleted": True, "topic": topic,
            "remaining_topics": list(profile.topics.keys())}


@mcp.tool()
def record_break(
    learner_id: str, topic: str, duration_minutes: Optional[int] = None
) -> dict:
    """
    Record that the learner took a break.
    Resets consecutive errors, sets post_break_warmup flag so the next question
    will be gentler, and updates break tracking.
    """
    topic = _normalize_topic(topic)
    profile = load_learner(learner_id)

    if topic not in profile.topics:
        return {"error": f"No data for topic '{topic}'. Start a session first."}

    ts = profile.topics[topic]
    bs = ts.break_state

    now = datetime.now().isoformat()
    bs.last_break_taken = now
    bs.breaks_taken += 1
    bs.consecutive_errors = 0
    bs.post_break_warmup = True
    bs.error_severity_trend = []

    # Update session summary
    if profile.session_history:
        profile.session_history[-1].breaks_taken += 1

    save_learner(profile)

    return {
        "recorded": True,
        "breaks_taken": bs.breaks_taken,
        "post_break_warmup": True,
        "message": (
            f"Break recorded. When you're ready, the next question will be "
            f"a warmup to ease back in."
        ),
    }


@mcp.tool()
def end_session(learner_id: str) -> dict:
    """
    End the current tutoring session.
    Saves session summary with duration and final stats.
    """
    profile = load_learner(learner_id)

    summary_data = {}
    if profile.session_history:
        current = profile.session_history[-1]
        current.end_time = datetime.now().isoformat()
        summary_data = {
            "session_number": current.session_number,
            "topic": current.topic,
            "duration_minutes": _session_duration_minutes(
                current.start_time, current.end_time
            ),
            "attempts": current.attempts_count,
            "correct": current.correct_count,
            "accuracy": (
                current.correct_count / current.attempts_count
                if current.attempts_count > 0
                else 0.0
            ),
            "mastery_start": current.mastery_start,
            "mastery_end": current.mastery_end,
            "mastery_change": current.mastery_end - current.mastery_start,
            "breaks_taken": current.breaks_taken,
        }

    save_learner(profile)

    return {"status": "session_ended", "summary": summary_data}


def _session_duration_minutes(start: str, end: str) -> float:
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end)
    return round((e - s).total_seconds() / 60, 1)


if __name__ == "__main__":
    mcp.run()
