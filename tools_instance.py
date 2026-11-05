import os
import platform


_lock_file = None


def acquire_instance_lock() -> tuple[bool, str]:
    """Hold a per-user OS file lock so only one agent can run at a time."""
    global _lock_file

    if platform.system() == "Darwin":
        base_dir = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Agatha AI")
    elif platform.system() == "Windows":
        base_dir = os.path.join(os.getenv("LOCALAPPDATA", os.path.expanduser("~")), "Agatha AI")
    else:
        base_dir = os.path.join(os.getenv("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state")), "agatha-ai")
    os.makedirs(base_dir, exist_ok=True)
    lock_path = os.path.join(base_dir, "agent.lock")
    handle = open(lock_path, "a+", encoding="utf-8")

    try:
        if platform.system() == "Windows":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(" ")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        handle.seek(0)
        owner = handle.read().strip() or "unknown process"
        handle.close()
        return False, f"Another Agatha instance already holds the user lock ({owner}). Stop it before launching a second instance."

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _lock_file = handle
    return True, ""
