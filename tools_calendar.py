import os
import shutil
import hashlib
from datetime import datetime, timedelta, timezone
import platform
import subprocess
import time
import uuid

from tools_macos_apps import run_applescript


def _calendar_event_script(start: datetime, duration_minutes: int) -> str:
    months = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
    script = '''
    on run argv
        set eventTitle to item 1 of argv
        set calendarName to item 2 of argv
        tell application "Calendar"
            if calendarName is "" then
                set targetCalendar to first calendar whose writable is true
            else
                set targetCalendar to calendar calendarName
                if writable of targetCalendar is false then error "The selected calendar is read-only."
            end if
            set eventStart to current date
            set day of eventStart to 1
            set month of eventStart to MONTH_NAME
            set year of eventStart to YEAR_VALUE
            set day of eventStart to DAY_VALUE
            set hours of eventStart to HOUR_VALUE
            set minutes of eventStart to MINUTE_VALUE
            set seconds of eventStart to 0
            set eventEnd to eventStart + DURATION_SECONDS
            set newEvent to make new event at end of events of targetCalendar with properties {summary:eventTitle, start date:eventStart, end date:eventEnd}
            set eventId to uid of newEvent
            if eventId is "" then error "Calendar did not return an event identifier."
            if summary of newEvent is not eventTitle then error "Calendar did not retain the event title."
            if start date of newEvent is not eventStart then error "Calendar did not retain the event start time."
            if end date of newEvent is not eventEnd then error "Calendar did not retain the event end time."
            return eventId
        end tell
    end run
    '''
    return (script.replace("MONTH_NAME", months[start.month - 1])
            .replace("YEAR_VALUE", str(start.year))
            .replace("DAY_VALUE", str(start.day))
            .replace("HOUR_VALUE", str(start.hour))
            .replace("MINUTE_VALUE", str(start.minute))
            .replace("DURATION_SECONDS", str(duration_minutes * 60)))


def _schedule_windows_outlook(title: str, start: datetime, duration_minutes: int) -> str:
    try:
        import win32com.client
        appointment = win32com.client.Dispatch("Outlook.Application").CreateItem(1)
        appointment.Subject = title
        appointment.Start = start
        appointment.Duration = duration_minutes
        appointment.Save()
        event_id = appointment.EntryID
        if not event_id:
            raise RuntimeError("Outlook did not return a saved appointment ID.")
        return f"Scheduled '{title}' in Outlook Calendar on {start:%Y-%m-%d %H:%M} (event ID: {event_id})."
    except ImportError:
        return _write_ics_event(title, start, duration_minutes,
                                "Outlook automation needs classic Outlook and pywin32; import this event file to add it.")
    except Exception as e:
        return _write_ics_event(title, start, duration_minutes,
                                f"Outlook did not confirm the save ({e}); import this event file to add it.")


def _schedule_android_calendar(title: str, start: datetime, duration_minutes: int) -> str:
    am_command = shutil.which("am") or ("/system/bin/am" if os.path.exists("/system/bin/am") else None)
    if not am_command:
        return _write_ics_event(title, start, duration_minutes,
                                "Android's calendar intent service is unavailable; import this event file on your device.")

    begin_ms = int(time.mktime(start.timetuple()) * 1000)
    end_ms = begin_ms + duration_minutes * 60 * 1000
    command = [
        am_command, "start", "-a", "android.intent.action.INSERT",
        "-d", "content://com.android.calendar/events",
        "--es", "title", title,
        "--el", "beginTime", str(begin_ms),
        "--el", "endTime", str(end_ms),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"Could not open Android Calendar: {e}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Android rejected the calendar intent."
        return f"Could not open Android Calendar: {detail}"
    return (f"Opened Android Calendar with '{title}' on {start:%Y-%m-%d %H:%M} prefilled. "
            "Review the details and tap Save; Android does not allow this desktop agent to save it silently.")


def _write_ics_event(title: str, start: datetime, duration_minutes: int, message: str,
                     include_alarm: bool = False, description: str = "") -> str:
    event_id = f"{uuid.uuid4()}@agatha-local"
    end = start + timedelta(minutes=duration_minutes)

    def escape_ics(value: str) -> str:
        return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Agatha AI//Calendar//EN",
        "CALSCALE:GREGORIAN", "BEGIN:VEVENT", f"UID:{event_id}",
        f"DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
        f"DTSTART:{start:%Y%m%dT%H%M%S}", f"DTEND:{end:%Y%m%dT%H%M%S}",
        f"SUMMARY:{escape_ics(title)}",
    ]
    if description.strip():
        lines.append(f"DESCRIPTION:{escape_ics(description.strip())}")
    if include_alarm:
        lines.extend(("BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder", "TRIGGER:PT0S", "END:VALARM"))
    lines.extend(("END:VEVENT", "END:VCALENDAR", ""))
    ics = "\r\n".join(lines)
    output_dir = os.path.join(os.path.expanduser("~"), "Documents", "Agatha Calendar")
    try:
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"{event_id.split('@')[0]}.ics")
        with open(output_path, "x", encoding="utf-8", newline="") as f:
            f.write(ics)
    except OSError as e:
        return f"Failed to save a portable calendar event: {e}"
    return f"{message} File saved at {output_path}; import it in your calendar app to finish."


# Simple calendar scheduling helper
def schedule_meeting(title: str, date_str: str, duration_minutes: int = 30) -> str:
    """Create and verify a native event in macOS Calendar.app.

    date_str format: 'YYYY-MM-DD HH:MM'
    """
    title = title.strip()
    if not title:
        return "Error: meeting title cannot be empty."
    try:
        start = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
        duration_minutes = int(duration_minutes)
    except (TypeError, ValueError):
        return "Error: date_str must be in format 'YYYY-MM-DD HH:MM'."
    if duration_minutes <= 0:
        return "Error: duration_minutes must be greater than zero."

    system = platform.system()
    if system == "Windows":
        return _schedule_windows_outlook(title, start, duration_minutes)
    if system == "Linux" and (os.getenv("ANDROID_ROOT") or os.path.exists("/system/bin/am")):
        return _schedule_android_calendar(title, start, duration_minutes)
    if system != "Darwin":
        return _write_ics_event(title, start, duration_minutes,
                                "This platform has no native Calendar adapter.")

    script = _calendar_event_script(start, duration_minutes)
    try:
        event_id = run_applescript(script, title, "")
    except Exception as e:
        return f"Failed to add '{title}' to Calendar.app: {e}"

    return (f"Scheduled '{title}' in Calendar.app on {start.strftime('%Y-%m-%d %H:%M')} "
            f"for {duration_minutes} minutes (event ID: {event_id}).")

def get_file_hash(filepath: str) -> str:
    """Generate SHA256 hash to detect duplicate files safely."""
    hasher = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            while chunk := f.read(8192):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return ""

def summarize_and_optimize_storage(target_dir: str = None) -> str:
    if not target_dir:
        target_dir = os.path.expanduser("~/Downloads")

    total, used, free = shutil.disk_usage("/")
    total_gb = round(total / (1024**3), 2)
    free_gb = round(free / (1024**3), 2)

    seen_hashes = {}
    duplicates = []
    large_files = []

    for root, _, files in os.walk(target_dir):
        for file in files:
            filepath = os.path.join(root, file)
            try:
                size_mb = os.path.getsize(filepath) / (1024 * 1024)
                if size_mb > 100:  # Flag files over 100 MB
                    large_files.append((file, round(size_mb, 1)))

                file_hash = get_file_hash(filepath)
                if file_hash:
                    if file_hash in seen_hashes:
                        duplicates.append(filepath)
                    else:
                        seen_hashes[file_hash] = filepath
            except Exception:
                continue

    summary = f"Disk Summary: {free_gb} GB free of {total_gb} GB total.\n"
    summary += f"Scanned folder: {target_dir}\n"
    summary += f"- Found {len(duplicates)} duplicate file(s) safely safe to review/delete.\n"
    summary += f"- Found {len(large_files)} large file(s) (>100MB).\n"
    if large_files:
        summary += f"  Top large files: {large_files[:3]}\n"

    return summary
