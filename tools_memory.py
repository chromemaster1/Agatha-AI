import functools
import json
import os
import threading
from datetime import datetime

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.getenv("AGATHA_DATA_DIR")
MEMORY_FILE = os.getenv("AGATHA_MEMORY_FILE", os.path.join(
    _DATA_DIR, "agent_memory.json") if _DATA_DIR else os.path.join(_BASE_DIR, "agent_memory.json"))
MAX_LOG_ENTRIES = 200  # keep the file bounded — old entries roll off automatically

_PROJECT_CATEGORY_KEYS = {"style": "style_notes", "preference": "preferences", "note": "notes"}

# Several specialist sub-agents can run at once and each one logs to this file, so every
# load -> modify -> save sequence holds one re-entrant lock, and saves are atomic
# (write a temp file, then replace) so a reader never sees half a file.
_LOCK = threading.RLock()


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _LOCK:
            return fn(*args, **kwargs)
    return wrapper


def _load() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return {"notes": [], "log": [], "projects": {}, "active_project": None, "insights": []}
    try:
        with open(MEMORY_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        return {"notes": [], "log": [], "projects": {}, "active_project": None, "insights": []}
    data.setdefault("notes", [])
    data.setdefault("log", [])
    data.setdefault("projects", {})
    data.setdefault("active_project", None)
    data.setdefault("insights", [])
    return data


def _save(data: dict):
    try:
        tmp = MEMORY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, MEMORY_FILE)
    except Exception as e:
        print(f"[Warning] Could not save agent memory: {e}")


def _ensure_project(data: dict, name: str):
    if name not in data["projects"]:
        data["projects"][name] = {
            "style_notes": [], "preferences": [], "notes": [],
            "created": datetime.now().isoformat(timespec="seconds"),
        }


# ---------------------------------------------------------------------------
# General durable notes (unscoped — not tied to a specific project)
# ---------------------------------------------------------------------------
@_locked
def remember_note(note: str) -> str:
    data = _load()
    data["notes"].append({"note": note, "added": datetime.now().isoformat(timespec="seconds")})
    _save(data)
    return f"I'll remember that: {note}"


def list_notes() -> str:
    data = _load()
    notes = data.get("notes", [])
    if not notes:
        return "I don't have anything specific saved in memory yet — say 'remember that ...' to add something."
    lines = [f"{i + 1}. {n['note']}" for i, n in enumerate(notes)]
    return "Here's what I remember:\n" + "\n".join(lines)


@_locked
def forget_note(index: int) -> str:
    data = _load()
    notes = data.get("notes", [])
    if not (1 <= index <= len(notes)):
        return f"There's no memory item numbered {index}. Say 'what do you remember' to see the current list."
    removed = notes.pop(index - 1)
    data["notes"] = notes
    _save(data)
    return f"Forgot: {removed['note']}"


@_locked
def clear_all_notes() -> str:
    data = _load()
    count = len(data.get("notes", []))
    data["notes"] = []
    _save(data)
    return f"Cleared {count} remembered note(s)."


# ---------------------------------------------------------------------------
# Named project / portfolio memory — style notes, preferences, and general
# notes scoped to a specific project, so "the client wants navy and gold"
# doesn't bleed into an unrelated project.
# ---------------------------------------------------------------------------
@_locked
def set_active_project(name: str) -> str:
    name = name.strip()
    if not name:
        return "Give the project a name, e.g. 'switch to project Acme Rebrand'."
    data = _load()
    is_new = name not in data["projects"]
    _ensure_project(data, name)
    data["active_project"] = name
    _save(data)
    return f"{'Created and switched' if is_new else 'Switched'} to project '{name}'."


def get_active_project():
    return _load().get("active_project")


def list_projects() -> str:
    data = _load()
    projects = data.get("projects", {})
    if not projects:
        return "No projects set up yet. Say 'switch to project <name>' to create one."
    active = data.get("active_project")
    lines = [f"- {name}{' (active)' if name == active else ''}" for name in projects]
    return "Projects:\n" + "\n".join(lines)


@_locked
def add_project_item(project: str, text: str, category: str = "note") -> str:
    data = _load()
    _ensure_project(data, project)
    key = _PROJECT_CATEGORY_KEYS.get(category, "notes")
    data["projects"][project].setdefault(key, []).append({
        "text": text, "added": datetime.now().isoformat(timespec="seconds")
    })
    data["projects"][project]["updated"] = datetime.now().isoformat(timespec="seconds")
    _save(data)
    label = {"style": "style note", "preference": "preference", "note": "note"}.get(category, "note")
    return f"Added {label} to '{project}': {text}"


def project_summary(project: str) -> str:
    data = _load()
    proj = data.get("projects", {}).get(project)
    if not proj:
        return f"No project named '{project}' found. Say 'list projects' to see what's available."
    lines = [f"Project: {project}"]
    if proj.get("style_notes"):
        lines.append("Style notes:")
        lines += [f"  - {n['text']}" for n in proj["style_notes"]]
    if proj.get("preferences"):
        lines.append("Preferences:")
        lines += [f"  - {n['text']}" for n in proj["preferences"]]
    if proj.get("notes"):
        lines.append("Notes:")
        lines += [f"  - {n['text']}" for n in proj["notes"]]
    if len(lines) == 1:
        lines.append("(nothing recorded for this project yet)")
    return "\n".join(lines)


@_locked
def delete_project(name: str) -> str:
    data = _load()
    if name not in data.get("projects", {}):
        return f"No project named '{name}' found."
    del data["projects"][name]
    if data.get("active_project") == name:
        data["active_project"] = None
    _save(data)
    return f"Deleted project '{name}'."


# ---------------------------------------------------------------------------
# Learned insights — the honest version of "the agent gets smarter over
# time": not model retraining (not possible from a local script calling
# external/local LLMs), but a growing set of behavioral notes captured from
# real interactions — explicit corrections, and periodic lightweight
# reflection on recent activity — fed back into every future system prompt.
# ---------------------------------------------------------------------------
MAX_INSIGHTS = 60


@_locked
def add_insight(text: str, source: str = "correction") -> str:
    data = _load()
    data["insights"].append({
        "text": text, "source": source, "added": datetime.now().isoformat(timespec="seconds")
    })
    data["insights"] = data["insights"][-MAX_INSIGHTS:]
    _save(data)
    return f"Learned: {text}"


def list_insights() -> str:
    data = _load()
    insights = data.get("insights", [])
    if not insights:
        return "I haven't learned anything specific from our interactions yet."
    lines = [f"{i + 1}. {n['text']}" for i, n in enumerate(insights)]
    return "Things I've learned from our interactions:\n" + "\n".join(lines)


@_locked
def clear_insights() -> str:
    data = _load()
    count = len(data.get("insights", []))
    data["insights"] = []
    _save(data)
    return f"Cleared {count} learned insight(s)."


# ---------------------------------------------------------------------------
# Rolling activity log
# ---------------------------------------------------------------------------
def log_event(summary: str):
    """Append a short entry to the rolling activity log. Called after every
    completed task/tool run so the agent has continuity across sessions.
    Never raises — logging failures should never block the task itself."""
    try:
        with _LOCK:
            data = _load()
            data["log"].append({"summary": summary, "at": datetime.now().isoformat(timespec="seconds")})
            data["log"] = data["log"][-MAX_LOG_ENTRIES:]
            _save(data)
    except Exception as e:
        print(f"[Warning] Could not log activity to memory: {e}")


def recent_log_text(n: int = 10) -> str:
    data = _load()
    log = data.get("log", [])[-n:]
    if not log:
        return "No activity logged yet."
    lines = [f"- [{e['at']}] {e['summary']}" for e in log]
    return "Recent activity:\n" + "\n".join(lines)


def memory_context_for_llm(max_notes: int = 20, max_log: int = 10, max_insights: int = 15) -> str:
    """Build a short text block to prepend to the system prompt so the LLM
    has continuity across sessions — remembered notes, active project
    context, learned insights, and a recap of recent completed tasks —
    instead of starting cold every run."""
    data = _load()
    notes = data.get("notes", [])[-max_notes:]
    log = data.get("log", [])[-max_log:]
    insights = data.get("insights", [])[-max_insights:]
    parts = []
    if notes:
        parts.append("Things the user has asked to be remembered:\n" +
                      "\n".join(f"- {n['note']}" for n in notes))
    active = data.get("active_project")
    if active and active in data.get("projects", {}):
        parts.append(f"Active project ('{active}') context:\n" + project_summary(active))
    if insights:
        parts.append("Things learned from past interactions (apply naturally, don't recite):\n" +
                      "\n".join(f"- {n['text']}" for n in insights))
    if log:
        parts.append("Recent activity from past sessions:\n" +
                      "\n".join(f"- {e['summary']}" for e in log))
    return "\n\n".join(parts)
