"""Local playback controls for macOS music apps and audio files."""
import os

from tools_macos_apps import run_applescript


_AUDIO_EXTENSIONS = {".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".ogg", ".wav"}
_PLAYERS = {"music": "Music", "apple music": "Music", "spotify": "Spotify"}
_CONTROL_SCRIPTS = {
    "play": "play",
    "pause": "pause",
    "stop": "stop",
    "next": "next track",
    "previous": "previous track",
}


def play_music(query: str = "", file_path: str = "", player: str = "music") -> str:
    """Play a local file or a library item in Music.app; resume a player's queue otherwise."""
    app = _PLAYERS.get((player or "music").strip().lower())
    if not app:
        return "Choose Music or Spotify as the playback app."

    if file_path:
        path = os.path.abspath(os.path.expanduser(file_path.strip().strip("\"'")))
        if not os.path.isfile(path):
            return f"I can't find the audio file '{file_path}'."
        if os.path.splitext(path)[1].lower() not in _AUDIO_EXTENSIONS:
            return "That file type isn't recognized as audio. Try MP3, M4A, WAV, FLAC, AAC, AIFF, or OGG."
        if app != "Music":
            return "Local audio files are opened in Music.app; use 'play music from <path>' or choose Music."
        script = """on run argv
    tell application "Music"
        activate
        open POSIX file (item 1 of argv)
        play
    end tell
end run"""
        run_applescript(script, path)
        return f"Playing {os.path.basename(path)} in Music."

    query = (query or "").strip()
    if query and app == "Music":
        script = """on run argv
    set songQuery to item 1 of argv
    tell application "Music"
        activate
        set matches to search playlist "Library" for songQuery only songs
        if (count of matches) is 0 then error "No matching song was found in the Music library."
        set chosenTrack to item 1 of matches
        play chosenTrack
        return name of chosenTrack
    end tell
end run"""
        try:
            title = run_applescript(script, query)
        except RuntimeError as error:
            return f"Couldn't play '{query}' from the Music library: {error}"
        return f"Playing {title or query} in Music."

    if query and app == "Spotify":
        if query.startswith("spotify:"):
            script = """on run argv
    tell application "Spotify" to play track (item 1 of argv)
end run"""
            run_applescript(script, query)
            return "Started the Spotify track."
        return "Spotify search isn't exposed to this local integration; open the track in Spotify, then ask me to resume or control playback."

    run_applescript(f'tell application "{app}" to activate\ntell application "{app}" to play')
    return f"Resumed playback in {app}."


def control_music(action: str, player: str = "music") -> str:
    app = _PLAYERS.get((player or "music").strip().lower())
    command = _CONTROL_SCRIPTS.get((action or "").strip().lower())
    if not app or not command:
        return "Choose a supported action (play, pause, stop, next, previous) and player (Music or Spotify)."
    run_applescript(f'tell application "{app}" to {command}')
    return f"{command.capitalize()} in {app}."


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


_STRING = {"type": "string"}
TOOL_SCHEMAS = [
    _schema(
        "play_music",
        "Play a local audio file or find a song in the user's Music library. Can resume Music or Spotify playback.",
        {"query": _STRING, "file_path": _STRING, "player": _STRING},
        [],
    ),
    _schema(
        "control_music",
        "Control local Music.app or Spotify playback: play, pause, stop, next, or previous.",
        {"action": _STRING, "player": _STRING},
        ["action"],
    ),
]

FUNCTIONS = {"play_music": play_music, "control_music": control_music}
TOOL_INTENTS = {
    r"\b(?:play|pause|resume|stop|skip|next|previous)\b.*\b(?:music|song|track|spotify|apple music)\b":
        {"play_music", "control_music"},
}
