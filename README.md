# Adaptive Tutor

An adaptive tutoring system that uses Claude CLI and MCP (Model Context Protocol) to deliver personalized, Socratic-style instruction with real-time mastery tracking.

## Architecture

```
Browser (index.html)
   │  WebSocket
   ▼
tutor_app.py  ──────►  claude CLI  ──────►  tutor_mcp_server.py
(FastAPI)               (subprocess)         (FastMCP server)
                                                  │
                                    ┌─────────────┼─────────────┐
                                    ▼             ▼             ▼
                            assessment_    learner_       ~/.claude/
                            engine.py      model.py       tutoring/
                            (pure logic)   (Pydantic +    learners/
                                            JSON I/O)     (profiles)
```

| File | Role |
|---|---|
| `tutor_app.py` | FastAPI backend — serves the UI, manages WebSocket connections, spawns `claude` subprocesses |
| `tutor_mcp_server.py` | MCP tool server — exposes tutoring tools (start_session, record_attempt, etc.) to Claude |
| `assessment_engine.py` | Pure-logic engine — trajectory, mastery, recommendations, break detection |
| `learner_model.py` | Pydantic data models + JSON persistence for learner profiles |
| `static/index.html` | Single-page web UI with KaTeX math rendering |
| `CLAUDE.md` | System prompt — pedagogical instructions for Claude |

## Prerequisites

- **Python 3.10+**
- **Claude CLI** with an active subscription — [install instructions](https://docs.anthropic.com/en/docs/claude-code)

## Installation

```bash
git clone <repo-url> adaptive-tutor
cd adaptive-tutor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Start the web server:

```bash
python3 tutor_app.py
```

Then open [http://localhost:8000](http://localhost:8000) in your browser.

The MCP server can also be run standalone for use with other Claude integrations:

```bash
python3 tutor_mcp_server.py
```

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `CLAUDE_CMD` | `claude` | Path to the Claude CLI binary |

## Learner Data

Learner profiles are stored at `~/.claude/tutoring/learners/` as JSON files, outside the project directory. No learner data is committed to the repository.

## Pedagogical Model

The system implements several evidence-based learning strategies:

- **Zone of Proximal Development (ZPD)**: Questions target one Bloom level above the learner's current mastery — challenging but achievable.
- **Mastery scoring**: Weighted recent accuracy with recency bias and confidence scaling. Scores range from 0.0 to 1.0.
- **Trajectory tracking**: Compares error severity across recent attempts to classify progress as improving, flat, or declining.
- **Error classification**: Errors are categorized as computational (right method, wrong calculation), structural (wrong method), or conceptual (misunderstanding the idea). Each triggers a different intervention.
- **Break detection**: Monitors consecutive errors, session duration, and error severity trends to suggest breaks when frustration signals appear.
- **Productive failure**: Computational errors (correct approach, wrong arithmetic) are allowed to repeat 2-3 times before intervention, as they represent productive practice.

## Running Tests

```bash
pip install -e ".[dev]"
pytest -v
```

## License

[MIT](LICENSE)
