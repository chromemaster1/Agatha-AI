import platform
import subprocess


def run_applescript(script: str, *arguments: str, timeout: int = 20) -> str:
    if platform.system() != "Darwin":
        raise RuntimeError("This integration requires macOS and Apple Events.")

    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script, "--", *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError("/usr/bin/osascript is unavailable on this Mac.") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("The macOS app did not respond in time. Check for a waiting app dialog.") from e

    if result.returncode == 0:
        return result.stdout.strip()

    error = result.stderr.strip() or "Unknown AppleScript error"
    lowered = error.lower()
    if "not authorized to send apple events" in lowered or "(-1743)" in lowered:
        raise RuntimeError(
            f"macOS denied access: {error}. Allow the app launching Agatha (such as Terminal or VS Code) "
            "to control the target app in System Preferences > Security & Privacy > Privacy > Automation, "
            "then retry."
        )
    raise RuntimeError(error)
