import json
import os

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_settings.json")

# Keys the Settings page knows how to manage. Grouped for display purposes.
SETTING_GROUPS = {
    "Bluesky": ["BLUESKY_IDENTIFIER", "BLUESKY_APP_PASSWORD"],
    "X / Twitter": ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"],
    "YouTube": ["YOUTUBE_CLIENT_SECRETS_FILE"],
    "Instagram": ["INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID"],
    "TikTok": ["TIKTOK_ACCESS_TOKEN"],
}

AI_AGENT_PROVIDERS = [
    {"id": "anthropic", "name": "Claude", "key": "ANTHROPIC_API_KEY",
     "model_key": "AGATHA_ANTHROPIC_MODEL", "default_model": "claude-sonnet-5-5"},
    {"id": "openai", "name": "ChatGPT", "key": "OPENAI_API_KEY",
     "model_key": "AGATHA_OPENAI_MODEL", "default_model": "gpt-4o-mini"},
    {"id": "gemini", "name": "Gemini", "key": "GEMINI_API_KEY",
     "model_key": "AGATHA_GEMINI_MODEL", "default_model": "gemini-2.5-flash"},
    {"id": "grok", "name": "Grok", "key": "GROK_API_KEY",
     "model_key": "AGATHA_GROK_MODEL", "default_model": "grok-3-mini"},
    {"id": "groq", "name": "Groq", "key": "GROQ_API_KEY",
     "model_key": "AGATHA_GROQ_MODEL", "default_model": "llama-3.3-70b-versatile"},
]
AI_AGENT_KEYS = [key for provider in AI_AGENT_PROVIDERS for key in (provider["key"], provider["model_key"])]
SOCIAL_KEYS = [key for keys in SETTING_GROUPS.values() for key in keys]
ALL_KEYS = SOCIAL_KEYS + AI_AGENT_KEYS


def _load() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    temporary_file = SETTINGS_FILE + ".tmp"
    with open(temporary_file, "w", encoding="utf-8") as settings_file:
        json.dump(data, settings_file, indent=2)
    os.chmod(temporary_file, 0o600)
    os.replace(temporary_file, SETTINGS_FILE)


def get_setting(key: str, default=None):
    """Settings file takes priority over .env, so a value set via the
    dashboard immediately overrides whatever (if anything) was in .env,
    without needing a restart."""
    data = _load()
    if data.get(key):
        return data[key]
    return os.getenv(key, default)


def set_setting(key: str, value: str) -> str:
    if key not in ALL_KEYS:
        raise ValueError("Unsupported settings key.")
    data = _load()
    if value:
        data[key] = value
    else:
        data.pop(key, None)
    _save(data)
    if value:
        os.environ[key] = value  # effective immediately for this running process too
    else:
        os.environ.pop(key, None)
    return f"Saved {key}." if value else f"Cleared {key}."


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return value[:4] + "…" + value[-2:]


def get_masked_settings() -> dict:
    """{key: {"set": bool, "masked": str}} — never returns the raw secret,
    so the dashboard frontend never actually holds a real credential value
    beyond the moment the user types it into a save request."""
    result = {}
    for key in ALL_KEYS:
        val = get_setting(key)
        result[key] = {"set": bool(val), "masked": _mask(val) if val else ""}
    return result


def get_agent_providers() -> list[dict]:
    providers = []
    for provider in AI_AGENT_PROVIDERS:
        key_value = get_setting(provider["key"])
        providers.append({
            **provider,
            "connected": bool(key_value),
            "masked": _mask(key_value) if key_value else "",
            "model": get_setting(provider["model_key"], provider["default_model"]),
        })
    return providers
