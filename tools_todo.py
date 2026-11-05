import json
import os

try:
    from tools_notes import sync_todos_to_notes
except Exception:
    sync_todos_to_notes = None

TODO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "todo_list.json")


def get_todos():
    if not os.path.exists(TODO_FILE):
        return []
    with open(TODO_FILE, "r") as f:
        todos = json.load(f)
    if not isinstance(todos, list):
        raise ValueError("To-do data must be a JSON list; no changes were made.")
    return todos


def _save_todos(todos):
    temp_file = TODO_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(todos, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_file, TODO_FILE)


def _sync_notes(todos) -> str:
    """Best-effort sync to Apple Notes so the user has somewhere visible to
    confirm/view the list. Never raises — a Notes sync failure shouldn't
    block the underlying to-do operation from succeeding."""
    if not sync_todos_to_notes:
        return ""
    try:
        return " " + sync_todos_to_notes(todos)
    except Exception as e:
        return f" (Could not sync to Notes: {e})"


def add_todo(task: str) -> str:
    task = task.strip()
    if not task:
        return "Error: task cannot be empty."
    try:
        todos = get_todos()
        todos.append({"task": task, "done": False})
        _save_todos(todos)
    except Exception as e:
        return f"Failed to save task locally: {e}"
    return f"Added task: '{task}' to your to-do list.{_sync_notes(todos)}"


def list_todos() -> str:
    todos = get_todos()
    _sync_notes(todos)  # keep the note current even on a plain list request
    if not todos:
        return "Your to-do list is empty."
    res = "Here are your tasks:\n"
    for idx, item in enumerate(todos, 1):
        status = "✓" if item["done"] else "✗"
        res += f"{idx}. [{status}] {item['task']}\n"
    return res
