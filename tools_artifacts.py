import json
import os
import uuid
from datetime import datetime

ARTIFACTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts.json")
MAX_ARTIFACTS = 300


def _load() -> list:
    if not os.path.exists(ARTIFACTS_FILE):
        return []
    try:
        with open(ARTIFACTS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _save(data: list):
    try:
        with open(ARTIFACTS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Warning] Could not save artifact manifest: {e}")


def record_artifact(kind: str, path: str, title: str = "") -> str:
    """kind: 'research_note' | 'research_pdf' | 'cleaned_code' | ...
    Returns the artifact's stable id."""
    data = _load()
    artifact_id = uuid.uuid4().hex[:12]
    data.append({
        "id": artifact_id,
        "kind": kind,
        "path": path,
        "title": title or os.path.basename(path),
        "created": datetime.now().isoformat(timespec="seconds"),
    })
    data = data[-MAX_ARTIFACTS:]
    _save(data)
    return artifact_id


def list_artifacts() -> list:
    return list(reversed(_load()))  # newest first


def get_artifact_path(artifact_id: str):
    """Look up a file path by artifact ID ONLY — never accept a raw path
    from a caller. This is the entire security boundary for serving
    artifact downloads over the dashboard's local HTTP server."""
    for a in _load():
        if a["id"] == artifact_id:
            return a["path"]
    return None
