import json
import os
from datetime import datetime

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.getenv("AGATHA_DATA_DIR")
SUBAGENTS_FILE = os.getenv("AGATHA_SUBAGENTS_FILE", os.path.join(
    _DATA_DIR, "subagents.json") if _DATA_DIR else os.path.join(_BASE_DIR, "subagents.json"))
MAX_HISTORY_PER_AGENT = 20


def _load() -> dict:
    if not os.path.exists(SUBAGENTS_FILE):
        return {}
    try:
        with open(SUBAGENTS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    try:
        with open(SUBAGENTS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Warning] Could not save sub-agents: {e}")


def create_subagent(name: str, purpose: str) -> str:
    name = name.strip()
    purpose = purpose.strip().rstrip(".?!")
    if not name or not purpose:
        return "Give the agent a name and a purpose, e.g. 'create an agent named Recipe Helper to suggest recipes from ingredients I list'."
    data = _load()
    key = name.lower()
    if key in data:
        return f"An agent named '{name}' already exists (purpose: {data[key]['purpose']}). Say 'delete agent {name}' first to redefine it."
    data[key] = {
        "name": name, "purpose": purpose,
        "created": datetime.now().isoformat(timespec="seconds"),
        "history": [],
    }
    _save(data)
    return f"Created agent '{name}' — purpose: {purpose}. Say \"ask {name} to <task>\" to use it."


def list_subagents() -> str:
    data = _load()
    if not data:
        return "No custom agents created yet. Say 'create an agent named <name> to <purpose>'."
    lines = [f"- {v['name']}: {v['purpose']}" for v in data.values()]
    return "Custom agents:\n" + "\n".join(lines)


def list_subagents_data() -> list:
    """Structured version for the dashboard."""
    data = _load()
    return [{"name": v["name"], "purpose": v["purpose"], "created": v["created"],
              "turns": len(v.get("history", []))} for v in data.values()]


def delete_subagent(name: str) -> str:
    data = _load()
    key = name.strip().lower()
    if key not in data:
        return f"No agent named '{name}' found. Say 'list agents' to see what's available."
    del data[key]
    _save(data)
    return f"Deleted agent '{name}'."


def get_subagent(name: str):
    return _load().get(name.strip().lower())


def append_subagent_turn(name: str, role: str, content: str):
    data = _load()
    key = name.strip().lower()
    if key not in data:
        return
    data[key].setdefault("history", []).append({"role": role, "content": content})
    data[key]["history"] = data[key]["history"][-MAX_HISTORY_PER_AGENT:]
    _save(data)


def reset_subagent_history(name: str) -> str:
    data = _load()
    key = name.strip().lower()
    if key not in data:
        return f"No agent named '{name}' found."
    data[key]["history"] = []
    _save(data)
    return f"Cleared '{name}''s conversation history — its purpose stays the same."
