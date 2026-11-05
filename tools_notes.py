import os
import platform
import re
import uuid

from tools_macos_apps import run_applescript

NOTE_TITLE = "Agent To-Do List"


def _run_applescript(script: str, timeout: int = 10) -> str:
    return run_applescript(script, timeout=timeout)


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _local_notes_dir() -> str:
    if os.getenv("ANDROID_ROOT") and os.path.isdir("/storage/emulated/0/Documents"):
        return "/storage/emulated/0/Documents/Agatha Notes"
    return os.path.join(os.path.expanduser("~"), "Documents", "Agatha Notes")


def _write_local_note(title: str, body: str, replace_existing: bool = False) -> str:
    os.makedirs(_local_notes_dir(), exist_ok=True)
    safe_title = re.sub(r"[^A-Za-z0-9 _-]", "", title).strip().replace(" ", "_")[:80] or "note"
    path = os.path.join(_local_notes_dir(), f"{safe_title}.md")
    temp_path = path + ".tmp"
    if os.path.exists(path) and not replace_existing:
        path = os.path.join(_local_notes_dir(), f"{safe_title}_{uuid.uuid4().hex[:8]}.md")
        temp_path = path + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n{body}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except OSError as e:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise RuntimeError(f"Could not save local note: {e}") from e
    return path


def sync_todos_to_notes(todos: list) -> str:
    """Create/update a note in Apple Notes titled 'Agent To-Do List' with the
    current to-do items, so the user has a visible place to confirm and
    review them outside the terminal. macOS + Notes.app only."""
    if platform.system() != "Darwin":
        lines = [f"{'[x]' if item.get('done') else '[ ]'} {item.get('task', '')}" for item in todos]
        path = _write_local_note(NOTE_TITLE, "\n".join(lines) if lines else "(No to-dos yet)", replace_existing=True)
        return f"Synced to local Markdown note at {path}."

    lines = []
    for item in todos:
        mark = "\u2611" if item.get("done") else "\u2610"  # ☑ / ☐
        lines.append(f"{mark} {item.get('task', '')}")
    body_text = "\n".join(lines) if lines else "(No to-dos yet)"

    script = f'''
    on run argv
        set noteTitle to item 1 of argv
        set noteBody to item 2 of argv
        tell application "Notes"
            set existingNotes to notes whose name is noteTitle
            if (count of existingNotes) > 0 then
                set body of item 1 of existingNotes to noteBody
            else
                make new note with properties {{name:noteTitle, body:noteBody}}
            end if
            if (count of (notes whose name is noteTitle)) is 0 then error "Notes did not retain the to-do note."
        end tell
    end run
    '''
    try:
        run_applescript(script, NOTE_TITLE, f"{NOTE_TITLE}\n\n{body_text}")
        return "Synced to Apple Notes."
    except Exception as e:
        return f"Could not sync to Apple Notes: {e}"


def create_note(title: str, body: str) -> str:
    """Create a native Apple Note or a local Markdown note, depending on OS."""
    title = title.strip()
    if not title:
        return "Error: note title cannot be empty."
    if not body.strip():
        return "Error: note content cannot be empty."
    if platform.system() != "Darwin":
        try:
            path = _write_local_note(title, body)
        except Exception as e:
            return f"Failed to create local note: {e}"
        return f"Created local Markdown note at {path}."
    script = '''
    on run argv
        set noteTitle to item 1 of argv
        set noteBody to item 2 of argv
        tell application "Notes"
            set newNote to make new note with properties {name:noteTitle, body:noteBody}
            set noteId to id of newNote
            if (count of (notes whose id is noteId)) is 0 then error "Notes did not retain the new note."
            return noteId
        end tell
    end run
    '''
    try:
        run_applescript(script, title, body)
    except Exception as e:
        return f"Failed to create note '{title}' in Notes.app: {e}"
    return f"Created and verified note '{title}' in Notes.app."
