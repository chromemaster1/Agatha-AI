import os
import sqlite3
import platform
import shutil
import tempfile

CHAT_DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")


def _messages_access_enabled() -> bool:
    return os.getenv("ALLOW_MESSAGES_ACCESS", "").strip().lower() in ("1", "true", "yes")


def fetch_recent_texts(count: int = 10) -> list:
    """Read the N most recent iMessage/SMS texts from the local Messages
    database, read-only, newest first, as [{sender, text}, ...].

    macOS only. Requires BOTH:
      1. ALLOW_MESSAGES_ACCESS=true in .env — explicit opt-in, since this
         touches personal message history and should never be silently
         enabled by default.
      2. Full Disk Access granted to whatever process runs Python, under
         System Settings > Privacy & Security > Full Disk Access.

    Messages stored only as rich 'attributedBody' (no plain text column
    value — common for reactions, some stickers/attachments-only messages)
    are skipped rather than guessed at, since reliably decoding that binary
    format is out of scope here. This never modifies the real database — it
    works on a temporary read-only copy and deletes it afterward."""
    if platform.system() != "Darwin":
        raise RuntimeError("Reading text messages is only available on macOS.")

    if not _messages_access_enabled():
        raise RuntimeError(
            "Text message access is disabled by default. To enable it, add "
            "ALLOW_MESSAGES_ACCESS=true to your .env file — this grants the agent "
            "read access to your personal Messages history, so only turn it on if "
            "you're comfortable with that."
        )

    if not os.path.exists(CHAT_DB_PATH):
        raise RuntimeError(f"Messages database not found at {CHAT_DB_PATH}.")

    count = max(1, min(int(count), 50))

    tmp_path = os.path.join(tempfile.gettempdir(), "agent_chat_readonly_copy.db")
    try:
        shutil.copy2(CHAT_DB_PATH, tmp_path)
    except PermissionError:
        raise RuntimeError(
            "Permission denied reading the Messages database. Grant Full Disk Access to "
            "your terminal/IDE under System Settings > Privacy & Security > Full Disk Access, "
            "then fully quit and restart that app (not just re-run the script)."
        )
    except Exception as e:
        raise RuntimeError(f"Could not copy the Messages database for read-only access: {e}")

    results = []
    try:
        conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT message.text, message.is_from_me, handle.id
            FROM message
            LEFT JOIN handle ON message.handle_id = handle.ROWID
            WHERE message.text IS NOT NULL AND message.text != ''
            ORDER BY message.date DESC
            LIMIT ?
            """,
            (count,),
        )
        for text, is_from_me, handle in cur.fetchall():
            sender = "Me" if is_from_me else (handle or "Unknown")
            results.append({"sender": sender, "text": text})
        conn.close()
    except Exception as e:
        raise RuntimeError(f"Could not read the Messages database: {e}")
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    return results
