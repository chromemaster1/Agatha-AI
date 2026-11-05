import subprocess
import platform


def _run_applescript(script: str, timeout: int = 20) -> str:
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Unknown AppleScript error")
        return result.stdout
    except FileNotFoundError:
        raise RuntimeError("osascript not found — email reading only works on macOS with Mail.app.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Mail.app did not respond in time — make sure it's open and an account is configured.")


def fetch_recent_emails(count: int = 5) -> list:
    """Return the N most recent emails from Mail.app's default inbox as
    [{sender, subject, snippet}, ...]. macOS + Mail.app only. Mail.app must
    be open with at least one account configured."""
    if platform.system() != "Darwin":
        raise RuntimeError("Email reading is only available on macOS (via Mail.app).")

    count = max(1, min(int(count), 25))  # sane bounds

    script = f'''
    tell application "Mail"
        set msgCount to count of messages of inbox
        if msgCount is 0 then return ""
        set upperBound to msgCount
        set lowerBound to (msgCount - {count} + 1)
        if lowerBound < 1 then set lowerBound to 1
        set theMessages to messages lowerBound thru upperBound of inbox
        set output to ""
        repeat with m in theMessages
            set theSender to ""
            set theSubject to ""
            set theSnippet to ""
            try
                set theSender to (sender of m)
            end try
            try
                set theSubject to (subject of m)
            end try
            try
                set fullContent to (content of m) as text
                if (length of fullContent) > 400 then
                    set theSnippet to (text 1 thru 400 of fullContent)
                else
                    set theSnippet to fullContent
                end if
            end try
            set output to output & theSender & "|||" & theSubject & "|||" & theSnippet & "@@@"
        end repeat
        return output
    end tell
    '''
    raw = _run_applescript(script)
    emails = []
    for chunk in raw.strip().split("@@@"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split("|||")
        if len(parts) == 3:
            sender, subject, snippet = parts
            emails.append({
                "sender": sender.strip(),
                "subject": subject.strip(),
                "snippet": snippet.replace("\n", " ").replace("\r", " ").strip(),
            })
    emails.reverse()  # newest first
    return emails
