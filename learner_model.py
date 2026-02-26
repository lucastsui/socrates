"""
Learner data models and JSON persistence.
Pydantic v2 models for the adaptive tutoring system.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

DATA_DIR = Path.home() / ".claude" / "tutoring" / "learners"


class Attempt(BaseModel):
    question_id: str
    learner_answer: str
    correct_answer: str
    is_correct: bool
    error_type: Optional[str] = None  # computational, structural, conceptual
    error_step: Optional[str] = None
    bloom_level: Optional[str] = None  # remember, understand, apply, analyze, evaluate, create
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class Misconception(BaseModel):
    description: str
    times_observed: int = 1
    resolved: bool = False
    first_seen: str = Field(default_factory=lambda: datetime.now().isoformat())
    last_seen: str = Field(default_factory=lambda: datetime.now().isoformat())


class ZPD(BaseModel):
    current_level: str = "remember"
    stretch_level: str = "understand"
    too_hard_level: str = "analyze"


class BreakState(BaseModel):
    session_start_time: Optional[str] = None
    last_break_suggestion: Optional[str] = None
    last_break_taken: Optional[str] = None
    breaks_taken: int = 0
    break_cooldown_minutes: int = 10
    consecutive_errors: int = 0
    post_break_warmup: bool = False
    error_severity_trend: list[int] = Field(default_factory=list)  # recent error severities


class TopicState(BaseModel):
    mastery_level: float = 0.0
    attempt_history: list[Attempt] = Field(default_factory=list)
    trajectory: str = "unknown"  # improving, flat, declining, unknown
    misconceptions: list[Misconception] = Field(default_factory=list)
    zpd: ZPD = Field(default_factory=ZPD)
    productive_failures: int = 0
    break_state: BreakState = Field(default_factory=BreakState)


class TopicGraph(BaseModel):
    prerequisites: dict[str, list[str]] = Field(default_factory=dict)


class SessionSummary(BaseModel):
    session_number: int
    topic: str
    start_time: str
    end_time: Optional[str] = None
    attempts_count: int = 0
    correct_count: int = 0
    mastery_start: float = 0.0
    mastery_end: float = 0.0
    breaks_taken: int = 0


class LearnerProfile(BaseModel):
    learner_id: str
    session_count: int = 0
    topics: dict[str, TopicState] = Field(default_factory=dict)
    topic_graphs: dict[str, TopicGraph] = Field(default_factory=dict)
    session_history: list[SessionSummary] = Field(default_factory=list)


def _learner_path(learner_id: str) -> Path:
    return DATA_DIR / f"{learner_id}.json"


def load_learner(learner_id: str) -> LearnerProfile:
    path = _learner_path(learner_id)
    if path.exists():
        data = json.loads(path.read_text())
        return LearnerProfile.model_validate(data)
    return LearnerProfile(learner_id=learner_id)


def save_learner(profile: LearnerProfile) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _learner_path(profile.learner_id)
    path.write_text(profile.model_dump_json(indent=2))
