from datetime import datetime

from tools_calendar import _write_ics_event


def schedule_reminder(title: str, date_str: str, notes: str = "") -> str:
    """Export a timed calendar alert; importing is required to activate it."""
    title = title.strip()
    if not title:
        return "Error: reminder title cannot be empty."
    try:
        due = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "Error: date_str must be in format 'YYYY-MM-DD HH:MM'."

    details = f"{notes.strip()}\n\n" if notes.strip() else ""
    return _write_ics_event(
        title,
        due,
        1,
        "Not scheduled yet: import this alert into a calendar app to activate it.",
        include_alarm=True,
        description=details,
    )
