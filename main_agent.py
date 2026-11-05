import base64
import json
import os
import re
import sys
import time
import difflib
import threading
import queue
import traceback
from types import SimpleNamespace
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from tools_todo import add_todo, list_todos
from tools_storage import summarize_and_optimize_storage
from tools_calendar import schedule_meeting
from tools_uploads import describe_attachments, transcribe_audio
from tools_notes import create_note
from tools_reminders import schedule_reminder
import tools_memory
import tools_subagents
import tools_security
from tools_instance import acquire_instance_lock

try:
    from tools_email import fetch_recent_emails
except Exception as e:
    print(f"[Info] Email reading unavailable ({e}).")
    fetch_recent_emails = None

try:
    from tools_messages import fetch_recent_texts
except Exception as e:
    print(f"[Info] Text message reading unavailable ({e}).")
    fetch_recent_texts = None

try:
    from tools_code import read_code_file, write_cleaned_code, strip_markdown_fences
except Exception as e:
    print(f"[Info] Code-assist tool unavailable ({e}).")
    read_code_file = write_cleaned_code = strip_markdown_fences = None

try:
    from tools_research import web_search_free, save_research_note
except Exception as e:
    print(f"[Info] Research tool unavailable ({e}).")
    web_search_free = None
    save_research_note = None

try:
    from tools_finance import fetch_market_data, TICKER_ALIASES
except Exception as e:
    print(f"[Info] Market analysis unavailable ({e}).")
    fetch_market_data = None
    TICKER_ALIASES = {}

try:
    from tools_content import PLATFORM_SPECS, default_platforms, normalize_platform_name
except Exception as e:
    print(f"[Info] Content adaptation unavailable ({e}).")
    PLATFORM_SPECS = {}
    default_platforms = lambda: []
    normalize_platform_name = lambda n: n

try:
    from tools_social import post_bluesky, post_x, post_youtube_video, post_instagram_image, post_tiktok_video
except Exception as e:
    print(f"[Info] Social posting unavailable ({e}).")
    post_bluesky = post_x = post_youtube_video = post_instagram_image = post_tiktok_video = None

try:
    import tools_persona
    import tools_profile
except Exception as e:
    print(f"[Info] Personality/profile layer unavailable ({e}).")
    tools_persona = tools_profile = None

try:
    import tools_creator
except Exception as e:
    print(f"[Info] Creator toolkit unavailable ({e}).")
    tools_creator = None

import tools_music

try:
    import tools_ads
except Exception as e:
    print(f"[Info] Ad connectors unavailable ({e}).")
    tools_ads = None

try:
    import tools_crew
except Exception as e:
    print(f"[Info] Specialist crew unavailable ({e}).")
    tools_crew = None


try:
    import dashboard
except Exception as e:
    print(f"[Info] Dashboard unavailable ({e}).")
    dashboard = None

if os.getenv("AGATHA_DASHBOARD", "true").strip().lower() in ("0", "false", "no", "off"):
    dashboard = None

try:
    from tools_artifacts import record_artifact
except Exception as e:
    print(f"[Info] Artifact tracking unavailable ({e}).")
    record_artifact = None


print(f"[Diagnostics] main_agent.py is running with interpreter: {sys.executable}")
print(f"[Diagnostics] Python version: {sys.version.split()[0]}")

# --- Safe voice I/O (falls back to text-only if mic/speakers aren't available) ---
try:
    from voice_io import (
        speak as _voice_speak,
        listen as _voice_listen,
        MicrophoneUnavailable,
        set_persona as _set_voice_persona,
        get_persona_status as _get_persona_status,
        list_voices_text as _list_voices_text,
        list_microphones_text as _list_microphones_text,
        test_microphone as _test_microphone,
        check_mac_mic_permission as _check_mac_mic_permission,
        CONFIG_FILE as _VOICE_CONFIG_FILE,
    )
except Exception as e:
    print(f"[Info] voice_io unavailable ({e}); using text I/O only.")
    _voice_speak = None
    _voice_listen = None
    MicrophoneUnavailable = Exception  # harmless placeholder so except clauses still work
    _set_voice_persona = None
    _get_persona_status = None
    _list_voices_text = None
    _list_microphones_text = None
    _test_microphone = None
    _check_mac_mic_permission = None
    _VOICE_CONFIG_FILE = None


AGENT_NAME = "Agatha"
_WAKE_NAME_PATTERN = r"(?:Agatha|Agata)" if AGENT_NAME == "Agatha" else re.escape(AGENT_NAME)
_WAKE_WORD_RE = re.compile(
    rf"^\s*(?:hey|hello)\s+{_WAKE_NAME_PATTERN}\b[\s,.:;!?-]*",
    re.IGNORECASE,
)


def _strip_wake_word(text: str) -> tuple[bool, str]:
    match = _WAKE_WORD_RE.match(text)
    if not match:
        return False, text
    return True, text[match.end():].strip()

# Tracks the agent's own last spoken line so the mic can recognize and
# discard its own voice being picked back up and mis-transcribed as a new
# user command — the actual cause of the 'repeats itself' behavior. This is
# a real limitation of using one device's speaker+mic together with no
# hardware echo cancellation; headphones/earbuds avoid it entirely, but
# this catches most of it in software too.
_last_spoken_text = ""
_last_spoken_lock = threading.Lock()


def _normalize_for_echo_check(s: str) -> str:
    return re.sub(r"[^\w\s]", "", (s or "")).lower().strip()


def _is_likely_self_echo(heard: str) -> bool:
    with _last_spoken_lock:
        last = _last_spoken_text
    if not last or not heard:
        return False
    h = _normalize_for_echo_check(heard)
    l = _normalize_for_echo_check(last)
    if not h or not l:
        return False
    ratio = difflib.SequenceMatcher(None, h, l).ratio()
    contained = h in l or l in h  # catches partial echoes (mic cut off mid-utterance)
    return ratio > 0.72 or contained

def speak(text: str):
    if dashboard and not dashboard.authenticated_event.is_set():
        return
    global _last_spoken_text
    print(f"Agent: {text}")
    with _last_spoken_lock:
        _last_spoken_text = text
    if dashboard:
        try:
            dashboard.add_log("Agent", text)
        except Exception:
            pass  # dashboard is a convenience layer — never let it block a response
    if tools_profile:
        try:
            tools_profile.log_turn("agent", text)
        except Exception:
            pass  # history is best-effort; never block a reply on it
    if _voice_speak:
        try:
            _voice_speak(text)
            # Brief cooldown after playback finishes, so any residual audio/
            # room echo has time to decay before the mic starts listening
            # again — reduces the window where self-echo can be captured.
            time.sleep(0.6)
        except Exception as e:
            print(f"[Warning] speak() failed, continuing in text mode: {e}")


# --- Optional Client Imports ---
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    import requests
except ImportError:
    requests = None


# --- Environment & Key Discovery ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
HF_TOKEN = os.getenv("HF_TOKEN")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
# Defaults are small-parameter tags on purpose (1.5B/1B/0.5B) — the untagged
# names (llama3.1, qwen2.5, mistral) resolve to ~7B models needing 4-5GB+ of
# RAM, which won't even load on a machine with only 1-2GB available. If you
# have more RAM to spare, override in .env with bigger tags, e.g.:
#   OLLAMA_MODEL=qwen2.5:7b
#   OLLAMA_FALLBACK_MODELS=llama3.1:8b,mistral:7b
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b")
# If the primary model crashes/isn't pulled/errors out, try these next in
# order, so one flaky/missing Ollama model doesn't fail the whole task.
OLLAMA_FALLBACK_MODELS = [
    m.strip() for m in os.getenv("OLLAMA_FALLBACK_MODELS", "llama3.2:1b,qwen2.5:0.5b").split(",") if m.strip()
]

openai_client = OpenAI(api_key=OPENAI_API_KEY) if (OPENAI_API_KEY and OpenAI) else None
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if (ANTHROPIC_API_KEY and anthropic) else None
groq_client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=GROQ_API_KEY) if (GROQ_API_KEY and OpenAI) else None
_gemini_client = None
_grok_client = None


def _configured_provider_clients():
    """Resolve dashboard-saved provider credentials at request time."""
    global _gemini_client, _grok_client
    openai_key = os.getenv("OPENAI_API_KEY")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    groq_key = os.getenv("GROQ_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    grok_key = os.getenv("GROK_API_KEY")
    clients = {
        "anthropic": (anthropic.Anthropic(api_key=anthropic_key) if anthropic_key and anthropic else None),
        "openai": (OpenAI(api_key=openai_key) if openai_key and OpenAI else None),
        "groq": (OpenAI(base_url="https://api.groq.com/openai/v1", api_key=groq_key)
                 if groq_key and OpenAI else None),
    }
    if gemini_key and OpenAI:
        _gemini_client = OpenAI(base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                                api_key=gemini_key)
    if grok_key and OpenAI:
        _grok_client = OpenAI(base_url="https://api.x.ai/v1", api_key=grok_key)
    clients["gemini"] = _gemini_client if gemini_key else None
    clients["grok"] = _grok_client if grok_key else None
    return clients


def _provider_model(provider: str, fallback: str) -> str:
    if provider == "anthropic":
        default = os.getenv("AGATHA_ANTHROPIC_MODEL", fallback)
    elif provider == "openai":
        default = os.getenv("AGATHA_OPENAI_MODEL", fallback)
    elif provider == "groq":
        default = os.getenv("AGATHA_GROQ_MODEL", fallback)
    elif provider == "gemini":
        default = "gemini-2.5-flash"
    else:
        default = "grok-3-mini"
    try:
        import tools_settings
        model_key = next((p["model_key"] for p in tools_settings.AI_AGENT_PROVIDERS if p["id"] == provider), "")
        return tools_settings.get_setting(model_key, default) if model_key else default
    except Exception:
        return default


def _linked_provider_ids() -> list[str]:
    try:
        import tools_settings
        return [provider["id"] for provider in tools_settings.get_agent_providers() if provider["connected"]]
    except Exception:
        return []


def _review_with_agent_council(user_input: str, answer: str, evidence: str = "") -> str:
    """Gather bounded reviews, then reconcile them against the user's request and tool evidence."""
    provider_ids = _linked_provider_ids()
    if len(provider_ids) < 2 or not answer:
        return answer

    clients = _configured_provider_clients()
    reviews = []
    review_prompt = (
        "Review this draft for concrete factual errors, unsupported claims, omissions, and relevance. "
        "Return only a concise correction or the exact word APPROVED. Do not invent details.\n\n"
        f"User request:\n{user_input[:2500]}\n\nDraft answer:\n{answer[:4000]}"
        + (f"\n\nTool result evidence (do not repeat sensitive values):\n{evidence[:5000]}" if evidence else "")
    )
    fallbacks = {
        "anthropic": _ANTHROPIC_MODEL["light"],
        "openai": _OPENAI_MODEL["light"],
        "gemini": "gemini-2.5-flash",
        "grok": "grok-3-mini",
        "groq": _GROQ_MODEL["light"],
    }
    for provider_id in provider_ids[:3]:
        client = clients.get(provider_id)
        if not client:
            continue
        try:
            if provider_id == "anthropic":
                response = client.messages.create(
                    model=_provider_model(provider_id, fallbacks[provider_id]), max_tokens=220,
                    system="You are a careful reviewer. Be specific and concise.",
                    messages=[{"role": "user", "content": review_prompt}],
                )
                review = "".join(block.text for block in response.content
                                 if getattr(block, "type", "") == "text")
            else:
                response = client.chat.completions.create(
                    model=_provider_model(provider_id, fallbacks[provider_id]),
                    messages=[{"role": "system", "content": "You are a careful, concise answer reviewer."},
                              {"role": "user", "content": review_prompt}],
                    max_tokens=220, temperature=0.1,
                )
                review = response.choices[0].message.content or ""
            review = review.strip()
            if review and review.casefold() != "approved":
                reviews.append(f"{provider_id}: {review[:1000]}")
        except Exception as error:
            print(f"[Info] {provider_id} council review unavailable: {error}")

    if not reviews:
        return answer
    if dashboard:
        dashboard.set_step("synthesizing", "Reconciling linked AI reviews")
    synthesis_messages = [
        {"role": "system", "content": (
            "You are Agatha, final editor of an AI review council. Preserve the draft when reviews are "
            "unsupported or disagree without evidence. Correct concrete errors, retain uncertainty, and "
            "answer the user directly. For actions, use tool result evidence as the only completion proof; "
            "if evidence is missing or failed, say so. Do not mention reviewers or claim consensus proves truth."
        )},
        {"role": "user", "content": (
            f"Request:\n{user_input}\n\nDraft:\n{answer}\n\nTool evidence:\n{evidence[:5000]}"
            f"\n\nReview notes:\n" + "\n\n".join(reviews)
        )},
    ]
    response = get_llm_response(synthesis_messages, tier="main")
    return answer if _llm_unavailable(response) else _clean_llm_text(response)

hf_pipe = None
# Default upgraded from flan-t5-small (weak, old, encoder-decoder — not
# built for open-ended conversation) to a small modern instruction-tuned
# chat model. Still CPU-friendly and fully offline once downloaded — this
# is the agent's genuine last-resort brain when neither a paid API nor
# Ollama is reachable, so it's worth it being an actual usable model.
local_model = os.getenv("LOCAL_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
_hf_load_attempted = False


def ensure_local_pipeline():
    global hf_pipe, _hf_load_attempted
    if hf_pipe is not None:
        return True
    if _hf_load_attempted:
        return False  # already tried and failed this session — don't retry every single message
    _hf_load_attempted = True
    try:
        from transformers import pipeline
    except ImportError as e:
        print(f"[Info] Local offline model unavailable — 'transformers'/'torch' not installed for THIS "
              f"interpreter ({sys.executable}): {e}\n"
              f"Install with:\n    {sys.executable} -m pip install transformers torch")
        hf_pipe = None
        return False
    # If a local path was provided, prefer that. Otherwise, try to use the
    # cached snapshot (via huggingface_hub) so repeated runs don't re-download
    # the model unnecessarily. Respect HF_TOKEN if present.
    model_id = local_model
    model_path = None
    try:
        # If the user passed a filesystem path, use it directly.
        if os.path.isdir(model_id):
            model_path = model_id
        else:
            try:
                from huggingface_hub import snapshot_download
                token = HF_TOKEN or os.getenv("HUGGINGFACE_HUB_TOKEN")
                print(f"[Info] Ensuring local snapshot of '{model_id}' (may download once)...")
                model_path = snapshot_download(repo_id=model_id, token=token)
            except Exception:
                # huggingface_hub may not be available or snapshot_download may fail;
                # fall back to letting transformers handle it (it will use the HF cache).
                model_path = None

        print(f"[Info] Loading local offline model '{model_id}'...")
        if model_path:
            hf_pipe = pipeline("text-generation", model=model_path)
        else:
            hf_pipe = pipeline("text-generation", model=model_id)
        print(f"[Info] Local offline model ready.")
        return True
    except Exception as e:
        print(f"[Warning] Failed to load local offline model '{model_id}': {e}")
        hf_pipe = None
        return False


def _last_user_text(messages) -> str:
    """The most recent actual USER message — NOT just messages[-1], which
    can be a tool result when this is called as the post-tool-call
    synthesis step. Using messages[-1] blindly was the bug behind the
    agent 'echoing' a tool's error text back as if it were something the
    user said."""
    for m in reversed(messages):
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role == "user":
            content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
            if content:
                return content
    return messages[-1].get("content", "") if messages else ""


def _trim_conversation_history(messages, max_turns: int = 12):
    """Keep system instructions and recent user turns, including tool exchanges."""
    system_messages = [
        message for message in messages
        if (message.get("role") if isinstance(message, dict) else getattr(message, "role", None)) == "system"
    ]
    turns = []
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        if role == "system":
            continue
        if role == "user" or not turns:
            turns.append([])
        turns[-1].append(message)
    recent_messages = [message for turn in turns[-max_turns:] for message in turn]
    return system_messages[:1] + recent_messages


# Two model tiers. Agatha herself ("main") talks to you; short-lived specialist sub-agents
# ("light") only pick a tool and fill in its arguments, so they run on a cheaper model.
# Override any of these in .env.
_ANTHROPIC_MODEL = {"main": os.getenv("AGATHA_ANTHROPIC_MODEL", "claude-sonnet-5-5"),
                    "light": os.getenv("AGATHA_LIGHT_ANTHROPIC_MODEL", "claude-haiku-5-5")}
_OPENAI_MODEL = {"main": os.getenv("AGATHA_OPENAI_MODEL", "gpt-4o-mini"),
                 "light": os.getenv("AGATHA_LIGHT_OPENAI_MODEL", "gpt-4o-mini")}
_GROQ_MODEL = {"main": os.getenv("AGATHA_GROQ_MODEL", "llama-3.3-70b-versatile"),
               "light": os.getenv("AGATHA_LIGHT_GROQ_MODEL", "llama-3.1-8b-instant")}
OLLAMA_LIGHT_MODEL = os.getenv("OLLAMA_LIGHT_MODEL", "")  # e.g. qwen2.5:1.5b even if OLLAMA_MODEL is bigger


def _ollama_models_to_try(tier: str = "main"):
    """Primary model first, then fallbacks, de-duplicated."""
    ordered = ([OLLAMA_LIGHT_MODEL] if (tier == "light" and OLLAMA_LIGHT_MODEL) else []) + [OLLAMA_MODEL] + OLLAMA_FALLBACK_MODELS
    seen = []
    for m in ordered:
        if m and m not in seen:
            seen.append(m)
    return seen


def ollama_server_alive(host=OLLAMA_HOST):
    """Checked once per get_llm_response call, not once per model — avoids
    hammering /api/tags for every fallback attempt."""
    if not requests:
        return False, []
    try:
        res = requests.get(f"{host}/api/tags", timeout=3)
        res.raise_for_status()
        return True, res.json().get("models", [])
    except Exception as e:
        print(f"[Info] Ollama server not reachable at {host} ({e}). Start it with 'ollama serve'.")
        return False, []


# --- OpenAI-format tools -> Anthropic tool spec ---
def _tools_to_anthropic(tools):
    if not tools:
        return None
    out = []
    for t in tools:
        fn = t.get("function", {})
        out.append({
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


def _messages_to_anthropic(messages):
    """Convert our OpenAI-style running transcript into Anthropic's format.
    Handles plain user/assistant text, assistant tool_calls, and tool results."""
    system_prompt = ""
    out = []
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)

        if role == "system":
            system_prompt = m.get("content", "")
            continue

        if role == "tool":
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id"),
                    "content": str(m.get("content", "")),
                }]
            })
            continue

        tool_calls = m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)
        if role == "assistant" and tool_calls:
            content_blocks = []
            text = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
            if text:
                content_blocks.append({"type": "text", "text": text})
            for tc in tool_calls:
                fn = tc.function if hasattr(tc, "function") else tc["function"]
                args = fn.arguments if hasattr(fn, "arguments") else fn["arguments"]
                try:
                    args = json.loads(args) if isinstance(args, str) else args
                except Exception:
                    args = {}
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.id if hasattr(tc, "id") else tc["id"],
                    "name": fn.name if hasattr(fn, "name") else fn["name"],
                    "input": args,
                })
            out.append({"role": "assistant", "content": content_blocks})
            continue

        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        attachments = m.get("attachments", []) if isinstance(m, dict) else []
        if role == "user" and attachments:
            blocks = [{"type": "text", "text": content or ""}]
            for attachment in attachments:
                image_data = attachment.get("image_base64")
                if image_data:
                    blocks.append({"type": "image", "source": {
                        "type": "base64",
                        "media_type": attachment["mime_type"],
                        "data": image_data,
                    }})
            out.append({"role": role, "content": blocks})
        else:
            out.append({"role": role, "content": content or ""})

    return system_prompt, out


def _messages_for_openai(messages):
    """Convert local attachments to OpenAI-compatible image content blocks."""
    converted = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user" or not message.get("attachments"):
            converted.append(message)
            continue
        content = [{"type": "text", "text": message.get("content", "")}]
        for attachment in message["attachments"]:
            image_data = attachment.get("image_base64")
            if image_data:
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:{attachment['mime_type']};base64,{image_data}"
                }})
        converted.append({"role": "user", "content": content})
    return converted


def _messages_for_ollama(messages):
    """Attach image bytes using Ollama's images field and omit local metadata."""
    converted = []
    for message in messages:
        if not isinstance(message, dict):
            converted.append(message)
            continue
        clean = {key: value for key, value in message.items() if key != "attachments"}
        images = [attachment["image_base64"] for attachment in message.get("attachments", [])
                  if attachment.get("image_base64")]
        if images:
            clean["images"] = images
        converted.append(clean)
    return converted


# --- Unified Multi-LLM Handler ---
# Internal sentinel prepended to the Echo Fallback's content so downstream
# code can distinguish "no LLM actually answered this" from a real reply.
# Never shown to the user directly — always stripped via _clean_llm_text().
NO_LLM_MARKER = "\x00NO_LLM_AVAILABLE\x00"


def get_llm_response(messages, tools=None, tool_choice="auto", tier="main"):
    """
    Tries providers in sequence:
    1. Anthropic Claude (Paid)   -- with real tool-calling support
    2. OpenAI (Paid)
    3. Groq (Free API Key)
    4. Ollama (Free Local Server) -- with real tool-calling support + readiness check
    5. Hugging Face Inference API / Local Pipeline (Free)
    6. Echo Fallback
    """

    configured_clients = _configured_provider_clients()

    # 1. Anthropic Claude
    active_anthropic = configured_clients.get("anthropic")
    if active_anthropic:
        try:
            system_prompt, anthropic_msgs = _messages_to_anthropic(messages)
            kwargs = dict(
                model=_provider_model("anthropic", _ANTHROPIC_MODEL.get(tier, _ANTHROPIC_MODEL["main"])),
                max_tokens=1024 if tier == "main" else 700,
                system=system_prompt,
                messages=anthropic_msgs,
            )
            anthropic_tools = _tools_to_anthropic(tools)
            if anthropic_tools:
                kwargs["tools"] = anthropic_tools

            response = active_anthropic.messages.create(**kwargs)

            text_parts = []
            tool_calls = []
            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_calls.append(SimpleNamespace(
                        id=block.id,
                        function=SimpleNamespace(name=block.name, arguments=json.dumps(block.input)),
                    ))

            msg = SimpleNamespace(content="".join(text_parts), tool_calls=tool_calls)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
        except Exception as e:
            print(f"[Warning] Claude API execution failed: {e}")

    # 2. OpenAI-compatible linked providers
    for provider_id, fallback in (
        ("openai", _OPENAI_MODEL.get(tier, _OPENAI_MODEL["main"])),
        ("gemini", "gemini-2.5-flash"),
        ("grok", "grok-3-mini"),
        ("groq", _GROQ_MODEL.get(tier, _GROQ_MODEL["main"])),
    ):
        client = configured_clients.get(provider_id)
        if not client:
            continue
        try:
            return client.chat.completions.create(
                model=_provider_model(provider_id, fallback),
                messages=_messages_for_openai(messages),
                tools=tools,
                tool_choice=tool_choice,
            )
        except Exception as e:
            print(f"[Warning] {provider_id} API execution failed: {e}")

    # 4. Ollama (Free Local Server e.g. http://localhost:11434), with
    #    multi-model fallback: if the primary model crashes, isn't pulled,
    #    or errors out, the next model in OLLAMA_FALLBACK_MODELS is tried
    #    automatically so one bad/missing model doesn't fail the task.
    if requests:
        alive, available_models = ollama_server_alive()
        if not alive:
            print("[Info] Ollama skipped: server not reachable.")
        else:
            available_names = [m.get("name", "") for m in available_models]
            for model_name in _ollama_models_to_try(tier):
                if not any(model_name in n for n in available_names):
                    print(f"[Info] Ollama model '{model_name}' not pulled (run: ollama pull {model_name}) — trying next fallback.")
                    continue
                try:
                    payload = {
                        "model": model_name,
                        "messages": _messages_for_ollama(_trim_conversation_history(messages)),
                        "stream": False,
                        "options": {
                            "temperature": 0.4,
                            "num_predict": _ollama_output_budget(tools),
                        },
                    }
                    if tools:
                        payload["tools"] = tools  # Ollama accepts OpenAI-style tool defs directly

                    res = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=60)
                    if res.status_code == 200:
                        data = res.json().get("message", {})
                        text = data.get("content", "")

                        tool_calls = []
                        for tc in data.get("tool_calls", []) or []:
                            fn = tc.get("function", {})
                            args = fn.get("arguments", {})
                            if not isinstance(args, str):
                                args = json.dumps(args)
                            tool_calls.append(SimpleNamespace(
                                id=tc.get("id", f"ollama-{len(tool_calls)}"),
                                function=SimpleNamespace(name=fn.get("name"), arguments=args),
                            ))

                        if model_name != OLLAMA_MODEL:
                            print(f"[Info] Used Ollama fallback model '{model_name}' (primary '{OLLAMA_MODEL}' was unavailable/failed).")

                        msg = SimpleNamespace(content=text, tool_calls=tool_calls)
                        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
                    else:
                        print(f"[Warning] Ollama model '{model_name}' returned status {res.status_code}: {res.text[:200]} — trying next fallback.")
                except Exception as e:
                    print(f"[Warning] Ollama model '{model_name}' crashed/failed: {e} — trying next fallback.")
                    continue

    # 5. Hugging Face Local Transformers (fully offline, no API/Ollama needed)
    if ensure_local_pipeline():
        # Build a proper chat-formatted conversation (role/content turns)
        # instead of just grabbing raw text — this lets the model actually
        # use recent context, and modern transformers pipelines handle
        # chat templates natively when given a list of role/content dicts.
        convo = []
        for m in messages[-8:]:
            role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
            content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
            if role in ("system", "user", "assistant") and content:
                convo.append({"role": role, "content": content})
        if not convo:
            convo = [{"role": "user", "content": _last_user_text(messages)}]

        try:
            out = hf_pipe(convo, max_new_tokens=250, do_sample=True, temperature=0.7)
            generated = out[0]["generated_text"]
            if isinstance(generated, list) and generated:
                last_turn = generated[-1]
                text = last_turn.get("content", "") if isinstance(last_turn, dict) else str(last_turn)
            else:
                text = str(generated)
        except Exception as e:
            text = f"(local offline model error: {e})"
        msg = SimpleNamespace(content=text, tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    # 6. Echo Fallback — every real provider was unreachable/unconfigured.
    # Note: local commands (todos, calendar, storage, notes, etc.) never
    # reach this path at all — only free-form requests that need an actual
    # LLM do, which is why this message explains what's missing rather
    # than just parroting the input back with no context.
    #
    # NO_LLM_MARKER is prepended so downstream code (the wrapper functions
    # below, and the main loop before speaking) can tell "this is the
    # fallback, not a real answer" apart from an actual LLM response —
    # without it, tools like assist_with_code were writing this exact
    # fallback text into a file as if it were real generated code.
    prompt = _last_user_text(messages)
    text = (NO_LLM_MARKER +
            "I don't have any LLM available right now to answer that — no paid API key is set, and "
            "Ollama isn't running or reachable. Start it with 'ollama serve', set OPENAI_API_KEY / "
            "ANTHROPIC_API_KEY / GROQ_API_KEY in your .env, or install the offline fallback model with "
            f"'{sys.executable} -m pip install transformers torch'. What you said was: " + prompt)
    msg = SimpleNamespace(content=text, tool_calls=[])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _strip_marker(content: str) -> str:
    content = content or ""
    if content.startswith(NO_LLM_MARKER):
        return content[len(NO_LLM_MARKER):]
    return content


def _llm_unavailable(response) -> bool:
    """True if get_llm_response() had to fall all the way back to the Echo
    Fallback — i.e. this is NOT a real answer from any LLM."""
    content = getattr(response.choices[0].message, "content", "") or ""
    return content.startswith(NO_LLM_MARKER)


def _clean_llm_text(response) -> str:
    """The response content with the internal marker stripped, safe to
    show/speak/save. Use this everywhere a response's content is displayed
    or written to a file, instead of reading .content directly."""
    return _strip_marker(response.choices[0].message.content)


# --- New capability wrappers: email/text summarizing, code assist, research ---
# These call get_llm_response() themselves, which is why they live below it.

def summarize_emails(count: int = 5) -> str:
    if not fetch_recent_emails:
        return "Email reading isn't available in this session (tools_email.py failed to load)."
    try:
        emails = fetch_recent_emails(count=count)
    except Exception as e:
        return f"Could not read emails: {e}"
    if not emails:
        return "No recent emails were found in the inbox."

    combined = "\n\n".join(
        f"From: {e['sender']}\nSubject: {e['subject']}\nPreview: {e['snippet']}" for e in emails
    )
    prompt = (
        f"Summarize these {len(emails)} recent emails concisely for a busy person. Group related "
        f"ones, and clearly call out anything urgent or needing a reply:\n\n{combined}"
    )
    response = get_llm_response([
        {"role": "system", "content": "You summarize emails concisely and flag anything needing action."},
        {"role": "user", "content": prompt},
    ])
    if _llm_unavailable(response):
        return f"Can't summarize your emails right now — {_clean_llm_text(response)}"
    return _clean_llm_text(response)


def summarize_texts(count: int = 10) -> str:
    if not fetch_recent_texts:
        return "Text message reading isn't available in this session (tools_messages.py failed to load)."
    try:
        texts = fetch_recent_texts(count=count)
    except Exception as e:
        return f"Could not read text messages: {e}"
    if not texts:
        return "No recent plain-text messages were found."

    combined = "\n".join(f"{t['sender']}: {t['text']}" for t in texts)
    prompt = f"Summarize these recent text messages concisely, noting anything that seems to need a reply:\n\n{combined}"
    response = get_llm_response([
        {"role": "system", "content": "You summarize text message threads concisely and note anything needing a reply."},
        {"role": "user", "content": prompt},
    ])
    if _llm_unavailable(response):
        return f"Can't summarize your texts right now — {_clean_llm_text(response)}"
    return _clean_llm_text(response)


def assist_with_code(file_path: str, instruction: str = None) -> str:
    if not read_code_file:
        return "Code assistance isn't available in this session (tools_code.py failed to load)."
    instruction = instruction or (
        "Clean up and improve this code: fix obvious bugs, improve readability and naming, and add "
        "brief comments where genuinely helpful — without changing its external behavior."
    )
    try:
        code = read_code_file(file_path)
    except Exception as e:
        return f"Could not read '{file_path}': {e}"

    prompt = (
        f"{instruction}\n\nHere is the current file content:\n\n```\n{code}\n```\n\n"
        f"Return ONLY the complete revised file content — no explanation, no markdown fences."
    )
    response = get_llm_response([
        {"role": "system", "content": "You are an expert software engineer helping clean up and improve code."},
        {"role": "user", "content": prompt},
    ])
    if _llm_unavailable(response):
        # This is the actual bug fix: previously this fallback text got
        # written straight into '<file>_cleaned.py' as if it were real
        # code. Now we bail out before touching the filesystem at all.
        return f"Can't clean up '{file_path}' right now — {_clean_llm_text(response)}"

    revised = strip_markdown_fences(_clean_llm_text(response))

    try:
        out_path = write_cleaned_code(file_path, revised)
    except Exception as e:
        return f"Generated the revised code but could not save it: {e}"

    if record_artifact:
        try:
            record_artifact("cleaned_code", out_path, title=os.path.basename(file_path))
        except Exception as e:
            print(f"[Warning] Could not record artifact: {e}")

    return f"Reviewed '{file_path}' and saved the cleaned-up version to '{out_path}'. Please review the diff before replacing the original."


def research_topic(topic: str, max_results: int = 6) -> str:
    global _last_research_topic
    _last_research_topic = topic.strip()
    if not web_search_free:
        return "Research isn't available in this session (tools_research.py / the 'ddgs' package failed to load)."
    try:
        results = web_search_free(topic, max_results=max_results)
    except Exception as e:
        return f"Research failed: {e}"
    if not results:
        return f"No results found for '{topic}'."

    combined = "\n\n".join(f"{r['title']}\n{r['url']}\n{r['snippet']}" for r in results)
    prompt = (
        f"Based on these search results, write a concise, well-organized research summary on "
        f"'{topic}'. Note any disagreement between sources, and don't invent facts not present "
        f"here:\n\n{combined}"
    )
    response = get_llm_response([
        {"role": "system", "content": "You synthesize web search results into a clear, accurate research summary, sticking to what the sources actually say."},
        {"role": "user", "content": prompt},
    ])
    if _llm_unavailable(response):
        # Don't save a fake "research note" containing just the fallback text.
        return f"Can't synthesize research on '{topic}' right now — {_clean_llm_text(response)}"
    summary = _clean_llm_text(response)

    if save_research_note:
        try:
            paths = save_research_note(topic, summary)
            note = f"\n\nSaved to: {paths['md_path']}"
            if paths.get("pdf_path"):
                note += f"\nPDF: {paths['pdf_path']}"
            if dashboard:
                note += "\nYou can also open and download it from the dashboard."
            summary += note
        except Exception as e:
            summary += f"\n\n(Could not save this research to a file: {e})"

    return summary


def analyze_market(symbol: str, period: str = "5d", interval: str = "1h") -> str:
    """Fetches real price data (free, via yfinance) and has the LLM describe
    what it shows factually. This is informational/technical description
    only — it never gives buy/sell advice or price predictions, and always
    carries a disclaimer, both by system-prompt instruction and appended
    here as a hard guarantee regardless of what the LLM produces."""
    if not fetch_market_data:
        return "Market analysis isn't available in this session (tools_finance.py / the 'yfinance' package failed to load)."
    try:
        data = fetch_market_data(symbol, period=period, interval=interval)
    except Exception as e:
        return f"Could not fetch market data: {e}"

    facts = (
        f"Symbol: {data['symbol']}\nLatest price: ${data['latest_price']}\n"
        f"Change over last {data['period']}: {data['period_pct_change']}%\n"
        f"Short-term moving average: {data['sma_short']}\nLonger moving average: {data['sma_long']}\n"
        f"RSI: {data['rsi']}"
    )
    prompt = (
        f"Here is recent technical market data:\n\n{facts}\n\n"
        f"Write a brief, factual technical description of what this data shows — trend direction, "
        f"momentum, and what the RSI value suggests about overbought/oversold conditions. Do NOT give "
        f"buy/sell advice, price targets, or predictions of any kind. Stick to describing the data itself."
    )
    response = get_llm_response([
        {"role": "system", "content": (
            "You are a market data analyst. You describe technical indicators factually and never give "
            "financial advice, recommendations, or price predictions of any kind."
        )},
        {"role": "user", "content": prompt},
    ])

    disclaimer = "\n\n(This is factual market data and technical description only — not financial advice.)"

    if _llm_unavailable(response):
        # Still hand back the real numbers even with no LLM to narrate them.
        return (
            f"{data['symbol']}: ${data['latest_price']} ({data['period_pct_change']}% over {data['period']}). "
            f"SMA(short)={data['sma_short']}, SMA(long)={data['sma_long']}, RSI={data['rsi']}.\n"
            f"(No LLM available for a narrative summary — {_clean_llm_text(response)})"
            f"{disclaimer}"
        )

    return _clean_llm_text(response) + disclaimer


def adapt_content_for_platforms(content: str, platforms: list = None) -> str:
    """Generate platform-tailored variants of one piece of content —
    pure text generation, no external calls, nothing published."""
    if not PLATFORM_SPECS:
        return "Content adaptation isn't available in this session (tools_content.py failed to load)."

    platforms = [normalize_platform_name(p) for p in (platforms or default_platforms())]
    valid = [p for p in platforms if p in PLATFORM_SPECS]
    invalid = [p for p in platforms if p not in PLATFORM_SPECS]
    if not valid:
        return f"None of the requested platforms are supported. Supported: {', '.join(PLATFORM_SPECS.keys())}."

    specs_text = "\n".join(
        f"- {PLATFORM_SPECS[p]['label']}: max {PLATFORM_SPECS[p]['char_limit']} characters, style: {PLATFORM_SPECS[p]['style']}"
        for p in valid
    )
    prompt = (
        f"Adapt the following content into a separate version for each platform listed below. Follow each "
        f"platform's character limit and style exactly. Label each version with the platform name as a "
        f"heading, and put a character count in parentheses right after each version.\n\n"
        f"Platforms:\n{specs_text}\n\nOriginal content:\n{content}"
    )
    response = get_llm_response([
        {"role": "system", "content": "You are a social media content strategist who adapts content for different platforms while respecting each platform's tone and length norms."},
        {"role": "user", "content": prompt},
    ])
    if _llm_unavailable(response):
        return f"Can't adapt this content right now — {_clean_llm_text(response)}"

    result = _clean_llm_text(response)
    if invalid:
        result += f"\n\n(Unsupported platform(s) ignored: {', '.join(invalid)}. Supported: {', '.join(PLATFORM_SPECS.keys())}.)"
    return result


# --- Social posting: staged with an explicit confirmation step. Nothing is
# ever published from a single ambiguous command — publishing is public and
# irreversible, so a separate 'confirm post' is always required. ---
_pending_post = {}

_SOCIAL_POSTERS = {
    "bluesky": lambda content, extra: post_bluesky(content) if post_bluesky else None,
    "x": lambda content, extra: post_x(content) if post_x else None,
    "twitter": lambda content, extra: post_x(content) if post_x else None,
    "instagram": lambda content, extra: post_instagram_image(extra.get("image_url", ""), content) if post_instagram_image else None,
    "youtube": lambda content, extra: post_youtube_video(extra.get("file_path", ""), extra.get("title", content[:70]), content) if post_youtube_video else None,
    "tiktok": lambda content, extra: post_tiktok_video(extra.get("file_path", ""), content) if post_tiktok_video else None,
}


def stage_social_post(platform: str, content: str, **extra) -> str:
    global _pending_post
    platform = normalize_platform_name(platform)
    if platform not in _SOCIAL_POSTERS:
        return f"Unsupported platform '{platform}'. Supported: bluesky, x, instagram, youtube, tiktok."
    _pending_post = {"platform": platform, "content": content, "extra": extra}
    preview = content if len(content) < 200 else content[:200] + "..."
    return f"Ready to post to {platform}: \"{preview}\". Say 'confirm post' to publish it, or 'cancel post' to discard it."


def confirm_social_post() -> str:
    global _pending_post
    if not _pending_post:
        return "There's no pending post to confirm."
    platform = _pending_post["platform"]
    content = _pending_post["content"]
    extra = _pending_post.get("extra", {})
    _pending_post = {}

    fn = _SOCIAL_POSTERS.get(platform)
    if not fn:
        return f"Unsupported platform '{platform}'."
    try:
        result = fn(content, extra)
        if result is None:
            return f"Posting to {platform} isn't available in this session (its module failed to load)."
        tools_memory.log_event(f"Posted to {platform}: {content[:100]}")
        return result
    except Exception as e:
        tools_memory.log_event(f"Post to {platform} FAILED: {e}")
        return f"Posting to {platform} failed: {e}"


def cancel_social_post() -> str:
    global _pending_post
    if not _pending_post:
        return "There's no pending post to cancel."
    _pending_post = {}
    return "Post cancelled — nothing was published."


# --- Plain LLM helper + creator-toolkit hooks. tools_creator / tools_profile stay
# decoupled from this file; they only get these callables via configure(). ---
def _plain_llm(system: str, prompt: str, tier: str = "main"):
    """One-shot LLM call with its own neutral prompt (no persona). None if no LLM is reachable."""
    response = get_llm_response([
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ], tier=tier)
    if _llm_unavailable(response):
        return None
    return _clean_llm_text(response)


def _generate_image_bytes(prompt: str, size: str = "1024x1024") -> bytes:
    if not openai_client:
        raise RuntimeError("OpenAI image generation requires OPENAI_API_KEY")
    response = openai_client.images.generate(model="gpt-image-1", prompt=prompt, size=size)
    image_data = response.data[0].b64_json
    if not image_data:
        raise RuntimeError("OpenAI returned no base64 image")
    return base64.b64decode(image_data)


def _creator_search(query: str):
    if not web_search_free:
        return []
    return web_search_free(query, max_results=5)


def _creator_poster(platform: str, content: str, extra: dict):
    fn = _SOCIAL_POSTERS.get(platform)
    return fn(content, extra) if fn else None


def _creator_notify(message: str):
    print(f"[Creator] {message}")
    if dashboard:
        try:
            dashboard.add_log("System", message)
        except Exception:
            pass
    tools_memory.log_event(f"Creator: {message[:200]}")


def run_subagent_task(name: str, task: str) -> str:
    agent = tools_subagents.get_subagent(name)
    if not agent:
        return f"No agent named '{name}' found. Say 'list agents' to see what's available, or 'create an agent named {name} to <purpose>' to make one."

    return _run_specialist_task(agent, task, history_name=name)


def delegate_to_specialist(purpose: str, task: str) -> str:
    """Run a one-off specialist task without persisting a named agent."""
    purpose = purpose.strip()
    if not purpose or not task.strip():
        return "A specialist needs both a focused purpose and a task."
    agent = {"name": purpose, "purpose": purpose, "history": []}
    return _run_specialist_task(agent, task)


def _run_specialist_task(agent: dict, task: str, history_name: str | None = None) -> str:
    system_prompt = (
        f"You are a specialized assistant named '{agent['name']}'. Your specific purpose: {agent['purpose']}. "
        f"Stay focused on this purpose. Break the task into its required outcomes, use available tools "
        f"when they can perform the work, inspect tool results, and never claim completion without a "
        f"successful result. Ask for essential missing information. Keep answers concise."
    )
    convo = [{"role": "system", "content": system_prompt}]
    convo.extend(agent.get("history", []))
    convo.append({"role": "user", "content": task})

    worker_tools = [
        tool for tool in tools
        if tool["function"]["name"] not in {
            "create_subagent", "run_subagent_task", "delegate_to_specialist", "list_subagents", "confirm_social_post"
        }
    ]
    worker_tool_names = {tool["function"]["name"] for tool in worker_tools}
    response = get_llm_response(convo, tools=worker_tools, tool_choice="auto")
    if _llm_unavailable(response):
        return f"Can't run '{agent['name']}' right now — {_clean_llm_text(response)}"

    tool_results = []
    for _ in range(6):
        response_message = response.choices[0].message
        pending_calls = getattr(response_message, "tool_calls", None) or []
        if not pending_calls:
            result = _clean_llm_text(response)
            break

        convo.append(response_message)
        for tool_call in pending_calls:
            tool_name = tool_call.function.name
            tool_output = _execute_tool_call(tool_call, allowed_functions=worker_tool_names)
            tool_results.append(f"{tool_name}: {tool_output}")
            convo.append({
                "tool_call_id": tool_call.id,
                "role": "tool",
                "name": tool_name,
                "content": tool_output,
            })
        response = get_llm_response(convo, tools=worker_tools, tool_choice="auto")
        if _llm_unavailable(response):
            result = "I completed these tool calls, but couldn't get a final summary: " + "; ".join(tool_results)
            break
    else:
        result = "Stopped after the tool-call limit. Results so far: " + "; ".join(tool_results)

    if any(_tool_output_failed(item.split(": ", 1)[-1]) for item in tool_results):
        result = "A delegated action failed; no completion is claimed. Results: " + "; ".join(tool_results)
    elif not tool_results and _looks_like_action_request(task):
        result = "No tool was run, so I haven't performed that action."
    if history_name:
        tools_subagents.append_subagent_turn(history_name, "user", task)
        tools_subagents.append_subagent_turn(history_name, "assistant", result)
    tools_memory.log_event(f"Specialist '{agent['name']}' ran task: {task[:100]}")
    return result


# --- "Learning" across interactions: honestly scoped — this is memory-based
# adaptation (captured corrections + periodic lightweight reflection fed
# back into future system prompts), not model retraining, which isn't
# something a local script calling external/local LLMs can do. ---
_CORRECTION_PATTERNS = (
    "no, i meant", "no i meant", "that's wrong", "thats wrong", "that's not right", "thats not right",
    "you misunderstood", "not what i asked", "not what i meant", "actually i meant", "i meant",
    "correct spelling", "correct name", "spelled", "i said", "wrong answer",
)
_last_research_topic = ""
_turns_since_reflection = 0
REFLECTION_INTERVAL = 12  # do a lightweight batch reflection roughly every N turns, not every single one


def _maybe_capture_correction(user_input: str, text: str):
    """Cheap, local, no LLM needed — captures the raw correction as an
    insight immediately. High-value, low-cost learning signal."""
    if any(p in text for p in _CORRECTION_PATTERNS):
        prior_reply = _last_spoken_text[:150] if _last_spoken_text else "(no prior reply recorded)"
        tools_memory.add_insight(
            f"User corrected a response. They said: \"{user_input}\". My prior reply was: \"{prior_reply}\"",
            source="correction",
        )


def _corrected_research_target(user_input: str) -> str | None:
    """Extract an explicitly corrected search name when the user asks to rerun research."""
    text = user_input.strip()
    lowered = text.lower()
    if not _last_research_topic or not web_search_free:
        return None
    if not any(word in lowered for word in ("search", "research", "look up", "rerun", "re-run", "again")):
        return None

    corrected = re.search(
        r"\b(?:i meant|i mean|actually,?\s+i meant|the correct (?:name|spelling) is|"
        r"correct (?:name|spelling) is|it's spelled|it is spelled|spelled)\s+"
        r"[\"'“”]?(.+?)[\"'“”]?(?=\s*[,.;!?]|\s+(?:not|rather|please|can you|could you|now|then|and)\b|$)",
        text,
        re.IGNORECASE,
    )
    if corrected:
        target = corrected.group(1).strip(" \t\r\n\"'“”.,;:!?-")
        return target or None

    contrast = re.search(r"\bnot\s+.+?\s+(?:but|rather)\s+(.+?)(?=[,.;!?]|$)", text, re.IGNORECASE)
    if contrast:
        target = contrast.group(1).strip(" \t\r\n\"'“”.,;:!?-")
        return target or None
    return None


def _maybe_run_periodic_reflection():
    """Infrequent, LLM-based: look at recent activity and distill 1-2
    general behavioral insights, rather than reflecting after every single
    turn (which would double the LLM calls for every interaction)."""
    global _turns_since_reflection
    _turns_since_reflection += 1
    if _turns_since_reflection < REFLECTION_INTERVAL:
        return
    _turns_since_reflection = 0

    recent = tools_memory.recent_log_text(15)
    if not recent or recent == "No activity logged yet.":
        return
    prompt = (
        f"Here is a recent activity log from an AI assistant's interactions with one user:\n\n{recent}\n\n"
        f"If there's a genuine, durable pattern worth remembering about how to serve this user better "
        f"(a preference, a recurring need, something to avoid), state it in ONE short sentence. "
        f"If there's nothing meaningfully new, respond with exactly: none"
    )
    try:
        response = get_llm_response([
            {"role": "system", "content": "You identify durable behavioral patterns from an activity log, concisely."},
            {"role": "user", "content": prompt},
        ])
        if _llm_unavailable(response):
            return
        insight = _clean_llm_text(response).strip()
        if insight and insight.lower() != "none" and len(insight) < 300:
            tools_memory.add_insight(insight, source="reflection")
    except Exception as e:
        print(f"[Info] Periodic reflection skipped: {e}")


_turns_since_profile_pass = 0
PROFILE_PASS_INTERVAL = 10  # an LLM pass over recent messages every N turns, on top of the free per-message rules


def _maybe_update_profile_llm():
    global _turns_since_profile_pass
    if not tools_profile:
        return
    _turns_since_profile_pass += 1
    if _turns_since_profile_pass < PROFILE_PASS_INTERVAL:
        return
    _turns_since_profile_pass = 0
    try:
        added = tools_profile.extract_with_llm(lambda s, p: _plain_llm(s, p, tier="light"))
        if added:
            tools_memory.log_event(f"Profile: learned {added} new thing(s) about the user.")
    except Exception as e:
        print(f"[Info] Profile update skipped: {e}")


def _meeting_date_reference(text: str, now: datetime):
    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if date_match:
        try:
            return datetime.strptime(date_match.group(1), "%Y-%m-%d"), True
        except ValueError:
            return None, True

    month_names = (
        "january|february|march|april|may|june|july|august|september|october|november|december"
        "|jan\\.?|feb\\.?|mar\\.?|apr\\.?|jun\\.?|jul\\.?|aug\\.?|sep\\.?|sept\\.?|oct\\.?|nov\\.?|dec\\.?"
    )
    month_first = re.search(
        rf"\b({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b",
        text,
        re.IGNORECASE,
    )
    day_first = re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_names})(?:,?\s+(20\d{{2}}))?\b",
        text,
        re.IGNORECASE,
    )
    if month_first or day_first:
        match = month_first or day_first
        if month_first:
            month_text, day_text, year_text = match.groups()
        else:
            day_text, month_text, year_text = match.groups()
        month_text = month_text.rstrip(".")
        try:
            month = datetime.strptime(month_text[:3], "%b").month
            year = int(year_text) if year_text else now.year
            day = datetime(year, month, int(day_text))
        except ValueError:
            return None, True
        if year_text is None and day.date() < now.date():
            try:
                day = day.replace(year=day.year + 1)
            except ValueError:
                return None, True
        return day, True

    if re.search(r"\btomorrow\b", text):
        return now + timedelta(days=1), True
    if re.search(r"\btoday\b", text):
        return now, True
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    for weekday, name in enumerate(weekdays):
        if re.search(rf"\b{name}\b", text):
            days_ahead = (weekday - now.weekday()) % 7 or 7
            return now + timedelta(days=days_ahead), True
    return None, False


def _parse_meeting(user_input: str, text: str, conversation=None):
    calendar_action = re.search(r"\b(?:schedule|create|book|add)\b.*\b(?:calendar|event)\b", text)
    meeting_action = re.search(r"\b(?:schedule|create|book|add)\b", text)
    if not calendar_action and not ("meeting" in text and meeting_action):
        return None

    now = datetime.now()
    day, date_found = _meeting_date_reference(text, now)
    if not date_found and conversation:
        prior_user_texts = [
            message.get("content", "")
            for message in conversation
            if isinstance(message, dict) and message.get("role") == "user"
        ]
        refers_to_prior_date = bool(re.search(
            r"\b(?:that date|same date|said date|date above|earlier date|date (?:we|i) "
            r"(?:discussed|mentioned|agreed|said|gave|told you))\b",
            text,
        ))
        relevant_history = prior_user_texts[-12:] if refers_to_prior_date else prior_user_texts[-1:]
        for previous_text in reversed(relevant_history):
            day, date_found = _meeting_date_reference(previous_text.lower(), now)
            if date_found:
                break
    if date_found and day is None:
        return "That date is invalid. Please give a valid calendar date."
    if day is None:
        return "What date should I schedule it for? Please say today, tomorrow, a weekday, or YYYY-MM-DD."

    clock_match = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", text)
    if clock_match:
        hour, minute = int(clock_match.group(1)), int(clock_match.group(2))
        ampm = clock_match.group(3)
    else:
        clock_match = re.search(r"\b(?:at\s+)?(\d{1,2})\s*(am|pm)\b", text)
        if not clock_match:
            if re.search(r"\bat\s+\d{1,2}\b", text):
                return "Is that time AM or PM? Please include AM/PM or use 24-hour time."
            return "What time should I schedule it for? Please include AM/PM or use 24-hour time."
        hour, minute, ampm = int(clock_match.group(1)), 0, clock_match.group(2)

    if ampm:
        if not 1 <= hour <= 12:
            return "That time is invalid. Please give a valid time with AM/PM."
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    elif not 0 <= hour <= 23:
        return "That time is invalid. Please use a valid 24-hour time."
    if minute > 59:
        return "That time is invalid. Please use minutes from 00 to 59."

    title_match = re.search(r"titled\s+['\"]([^'\"]+)['\"]", user_input, re.IGNORECASE)
    title = title_match.group(1).strip() if title_match else ""
    if not title:
        participant = re.search(
            r"\b(?:meeting\s+(?:with|about)|with)\s+(.+?)(?=\s+(?:today|tomorrow|on|at|next|\d{4}-\d{2}-\d{2})\b|$)",
            user_input,
            re.IGNORECASE,
        )
        if participant:
            title = f"Meeting with {participant.group(1).strip()}"
        else:
            scheduled_title = re.search(r"\b(?:schedule|create|book)\s+(?:a\s+)?(.+?)\s+meeting\b", user_input, re.IGNORECASE)
            if scheduled_title and scheduled_title.group(1).strip().lower() not in ("a", "the"):
                title = f"{scheduled_title.group(1).strip()} meeting"
    if not title:
        return "What should I call this meeting? You can say its title in quotes."

    start_dt = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    date_str = start_dt.strftime("%Y-%m-%d %H:%M")

    dur_match = re.search(r"(\d+)\s*(minutes|minute|min)", text)
    duration = int(dur_match.group(1)) if dur_match else 30

    return schedule_meeting(title=title, date_str=date_str, duration_minutes=duration)


def _parse_reminder(user_input: str, text: str):
    if not re.search(r"\b(?:remind me|reminder|alarm|wake me)\b", text):
        return None
    has_alarm_intent = bool(re.search(r"\b(?:alarm|wake me|set (?:an? )?reminder)\b", text))
    has_time = bool(re.search(r"\b\d{1,2}(?::\d{2}\s*(?:am|pm)?|\s*(?:am|pm))\b|\b(?:at|for)\s+\d{1,2}\b", text))
    if "remind me" in text and not has_alarm_intent and not has_time:
        return None
    if not re.search(r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|20\d{2}-\d{2}-\d{2})\b", text):
        return "What date should I set the reminder for? Say today, tomorrow, a weekday, or YYYY-MM-DD."

    now = datetime.now()
    day = None
    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    try:
        if date_match:
            day = datetime.strptime(date_match.group(1), "%Y-%m-%d")
        elif "tomorrow" in text:
            day = now + timedelta(days=1)
        elif "today" in text:
            day = now
        else:
            weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
            for weekday, name in enumerate(weekdays):
                if re.search(rf"\b{name}\b", text):
                    day = now + timedelta(days=(weekday - now.weekday()) % 7 or 7)
                    break
    except ValueError:
        return "That date is invalid. Please give the date as YYYY-MM-DD."

    clock_match = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", text)
    if clock_match:
        hour, minute, ampm = int(clock_match.group(1)), int(clock_match.group(2)), clock_match.group(3)
    else:
        clock_match = re.search(r"\b(?:at\s+|for\s+)?(\d{1,2})\s*(am|pm)\b", text)
        if not clock_match:
            if re.search(r"\b(?:at|for)\s+\d{1,2}\b", text):
                return "Is that time AM or PM? Please include AM/PM or use 24-hour time."
            return "What time should I set the reminder for? Include AM/PM or use 24-hour time."
        hour, minute, ampm = int(clock_match.group(1)), 0, clock_match.group(2)

    if ampm:
        if not 1 <= hour <= 12:
            return "That time is invalid. Please give a valid time with AM/PM."
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    elif not 0 <= hour <= 23:
        return "That time is invalid. Please use a valid 24-hour time."
    if minute > 59:
        return "That time is invalid. Please use minutes from 00 to 59."

    task_match = re.search(r"\bremind me to\s+(.+?)(?=\s+(?:today|tomorrow|on|at|next|20\d{2}-\d{2}-\d{2})\b|$)", user_input, re.IGNORECASE)
    if task_match:
        title = task_match.group(1).strip(" .,!?")
    elif re.search(r"\b(?:alarm|wake me)\b", text):
        title = "Alarm"
    else:
        title = ""
    if not title:
        return "What should I remind you about?"

    due = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return schedule_reminder(title, due.strftime("%Y-%m-%d %H:%M"))


def _parse_note(user_input: str, text: str):
    match = re.search(
        r"\b(?:create|save|write|take)\s+(?:a\s+)?note(?:\s+(?:titled|called)\s+['\"]([^'\"]+)['\"])?\s*(?:saying|that|:|about)\s+(.+)",
        user_input,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    body = match.group(2).strip()
    title = (match.group(1) or body.splitlines()[0][:60]).strip()
    return create_note(title, body)


def _parse_todo(user_input: str, text: str):
    has_todo_word = any(w in text for w in ("todo", "to-do", "to do"))
    has_remind_word = "remind me" in text
    list_triggers = ("list", "show", "pull up", "what's on", "whats on", "see my", "view my", "check my")
    if has_todo_word and re.search(r"\b(?:create|make|build)\b.*\b(?:list|todo|to-do)\b", text):
        items_match = re.search(r"\b\d+\s+(?:things|items|tasks)\s*[,;:]?\s*(.+)$", user_input,
                                re.IGNORECASE | re.DOTALL)
        if items_match:
            items = [item.strip(" \t\r\n,;:-") for item in
                     re.split(r"(?:^|[,;])\s*\d+[.)]?\s*", items_match.group(1))]
            items = [item for item in items if item]
            if items:
                results = [add_todo(task=item) for item in items]
                return "\n".join(results)
    if has_todo_word and any(t in text for t in list_triggers):
        return list_todos()

    m = re.search(r"(?:add|remind me to|todo)\s*[:\-]?\s*(.+)", text)
    if (has_todo_word or has_remind_word) and m:
        task = m.group(1).strip()
        # Avoid mis-firing on something like "check my to do list" if it
        # somehow reaches here — only treat it as an add if there's a real
        # trailing task, not just the word 'list' itself.
        if task and task not in ("list", "list.", "list?"):
            return add_todo(task=task)
    return None


def _parse_storage(user_input: str, text: str, target_dir=None):
    storage_keywords = ["storage", "disk space", "free up space", "clean up", "optimi"]
    if any(k in text for k in storage_keywords):
        return summarize_and_optimize_storage(target_dir=target_dir)
    return None


def _parse_voice_settings(user_input: str, text: str):
    """Local commands to change/inspect the agent's TTS persona and mic, e.g.:
      'set voice to female', 'use voice 2', 'set tone to warm',
      'list voices', 'list microphones', 'what voice are you using'."""
    if _set_voice_persona is None:
        return None  # voice_io unavailable in this session

    if "check" in text and ("permission" in text or "mic permission" in text):
        if not _check_mac_mic_permission:
            return "Microphone permission checking isn't available in this session."
        status, message = _check_mac_mic_permission()
        return f"macOS microphone permission status: {status}. {message}"

    if "test microphone" in text or "test mic" in text or "mic test" in text:
        return _test_microphone() if _test_microphone else "Microphone testing isn't available in this session."

    if "list microphone" in text or "list mic" in text:
        return _list_microphones_text()

    if "list voices" in text or "what voices" in text:
        return _list_voices_text()

    if "what voice" in text or "current voice" in text or "voice status" in text:
        return _get_persona_status()

    if "voice" not in text and "tone" not in text:
        return None

    # Explicit index selection, e.g. "use voice 2" / "set voice index 1"
    idx_match = re.search(r"voice\s*(?:number|index|#)?\s*(\d+)", text)
    if idx_match:
        return _set_voice_persona(index=int(idx_match.group(1)))

    gender = None
    if re.search(r"\bfemale\b", text):
        gender = "female"
    elif re.search(r"\bmale\b", text):
        gender = "male"

    tone = None
    for t in ["warm", "calm", "energetic", "deep", "bright", "neutral"]:
        if re.search(rf"\b{t}\b", text):
            tone = t
            break

    if gender or tone:
        return _set_voice_persona(gender=gender, tone=tone)

    return None


def _parse_public_voice_mode(user_input: str, text: str):
    if re.search(r"\b(?:enable|turn on|start)\b.*\bpublic\b.*\b(?:voice|listening)\b", text):
        _PUBLIC_VOICE_MODE.set()
        return ("Public voice focus is on. I will ignore speech unless it starts with 'Hey Agatha'. "
                "This is wake-word filtering, not biometric speaker identification; anyone using the wake phrase may be heard.")
    if re.search(r"\b(?:disable|turn off|stop)\b.*\bpublic\b.*\b(?:voice|listening)\b", text):
        _PUBLIC_VOICE_MODE.clear()
        return "Public voice focus is off. Voice input is back to normal listening."
    return None


def _parse_security_review(user_input: str, text: str, attachments=None):
    risk_intent = re.search(
        r"\b(?:scam|scams|phish(?:ing)?|malware|virus|compromised|fake|forged|authentic|authenticate|"
        r"certificate|certification|proof|verify|verification|security|safe)\b", text)
    inspect_target = re.search(
        r"\b(?:scan|inspect|review|check)\b.{0,50}\b(?:link|site|file|document|image|code|certificate)\b", text)
    if not risk_intent and not inspect_target:
        return None

    reports = []
    urls = list(dict.fromkeys(_URL_RE.findall(user_input)))
    urls.extend(item.get("url", "") for item in attachments or [] if item.get("url"))
    if not urls:
        domain = re.search(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", user_input, re.IGNORECASE)
        if domain:
            urls.append("https://" + domain.group(0))
    for url in dict.fromkeys(urls):
        reports.append(tools_security.review_safety(url=url))

    for attachment in attachments or []:
        path = attachment.get("path")
        subject = user_input or attachment.get("name", "uploaded file")
        if path:
            reports.append(tools_security.review_safety(
                file_path=path, content=attachment.get("text", ""), subject=subject))
        elif attachment.get("text"):
            reports.append(tools_security.review_safety(
                content=attachment["text"], subject=subject))

    if not reports:
        path_match = re.search(r"(?:\"([^\"]+)\"|'([^']+)'|((?:~|/)[^\s]+))", user_input)
        if path_match:
            path = next(value for value in path_match.groups() if value)
            reports.append(tools_security.review_safety(file_path=path, subject=user_input))
        elif re.search(r"```|\b(?:eval|exec|os\.system|subprocess|pickle\.load|yaml\.load)\s*\b", user_input):
            reports.append(tools_security.review_safety(content=user_input, subject="pasted code"))
        elif len(user_input.strip()) > 80:
            reports.append(tools_security.review_safety(content=user_input, subject=user_input))

    if reports:
        return "\n\n".join(reports)
    return "Send the link, attach the file or document, or paste the text/code you want me to review."


def _parse_email(user_input: str, text: str):
    if not fetch_recent_emails:
        return None
    if any(p in text for p in ["summarize my email", "read my email", "check my email", "check email", "summarize email"]):
        count_match = re.search(r"(\d+)\s*email", text)
        count = int(count_match.group(1)) if count_match else 5
        return summarize_emails(count=count)
    return None


def _parse_texts(user_input: str, text: str):
    if not fetch_recent_texts:
        return None
    if any(p in text for p in ["summarize my text", "read my text", "read my message", "check my message", "summarize my message"]):
        count_match = re.search(r"(\d+)\s*(text|message)", text)
        count = int(count_match.group(1)) if count_match else 10
        return summarize_texts(count=count)
    return None


def _parse_code_assist(user_input: str, text: str):
    if not read_code_file:
        return None
    if not any(p in text for p in ["clean up code", "clean up the code", "review code", "review this code",
                                    "fix code", "improve code", "refactor"]):
        return None
    # Look for a path-like token: contains a slash, or ends with a common
    # code file extension.
    path_match = re.search(r"([\w./~\-]+\.(py|js|ts|jsx|tsx|java|go|rb|c|cpp|h|swift|rs|php|html|css))\b", user_input)
    if not path_match:
        path_match = re.search(r"([\w./~\-]*/[\w./~\-]+)", user_input)
    if not path_match:
        return "I heard a code cleanup request but couldn't find a file path in it — try 'clean up code in <path>'."
    file_path = path_match.group(1)
    return assist_with_code(file_path)


def _parse_research(user_input: str, text: str):
    if not web_search_free:
        return None
    match = re.search(r"\bresearch\s+(.+)", user_input, re.IGNORECASE)
    if not match:
        return None
    topic = match.group(1).strip().rstrip(".?!")
    if not topic:
        return None
    return research_topic(topic)


def _extract_market_symbol(user_input: str, text: str):
    for name, ticker in TICKER_ALIASES.items():
        if name in text:
            return ticker
    # Fall back to an explicit ticker-looking token, e.g. "AAPL" or "BTC-USD"
    m = re.search(r"\b([A-Z]{2,6}(?:-USD)?)\b", user_input)
    if m and m.group(1).upper() not in ("I", "A", "OK"):
        return m.group(1).upper()
    return None


def _parse_market(user_input: str, text: str):
    if not fetch_market_data:
        return None
    triggers = ("market analysis", "market chart", "stock price", "crypto price",
                "price of", "run analysis on", "candle", "technical analysis")
    if not any(t in text for t in triggers):
        return None
    symbol = _extract_market_symbol(user_input, text)
    if not symbol:
        return ("I heard a market analysis request but couldn't tell which asset — try "
                "'run market analysis on Bitcoin' or 'AAPL price'.")
    return analyze_market(symbol)


def _parse_memory(user_input: str, text: str):
    # --- Named project / portfolio memory ---
    proj_switch = re.search(r"(?:switch to|start|create)\s+project\s+(.+)", user_input, re.IGNORECASE)
    if proj_switch:
        return tools_memory.set_active_project(proj_switch.group(1).strip().rstrip(".?!"))

    if "list projects" in text or "what projects" in text:
        return tools_memory.list_projects()

    if "project summary" in text or "about this project" in text or "current project" in text:
        active = tools_memory.get_active_project()
        if not active:
            return "No active project is set. Say 'switch to project <name>' first."
        return tools_memory.project_summary(active)

    delete_proj = re.search(r"delete project\s+(.+)", user_input, re.IGNORECASE)
    if delete_proj:
        return tools_memory.delete_project(delete_proj.group(1).strip().rstrip(".?!"))

    for category, phrases in (
        ("style", ["style note", "remember this style"]),
        ("preference", ["preference note", "remember this preference"]),
        ("note", ["project note", "remember for this project"]),
    ):
        for phrase in phrases:
            m = re.search(rf"{re.escape(phrase)}[:\s]+(.+)", user_input, re.IGNORECASE)
            if m:
                active = tools_memory.get_active_project()
                if not active:
                    return "No active project is set — say 'switch to project <name>' first."
                return tools_memory.add_project_item(active, m.group(1).strip().rstrip(".?!"), category)

    # --- General unscoped notes ---
    if re.search(r"\bremember\s+(that\s+)?(.+)", text) and not re.search(r"\b(?:do|did|can|could|would|will)\s+you\s+remember\b", text):
        m = re.search(r"\bremember\s+(?:that\s+)?(.+)", user_input, re.IGNORECASE)
        note = m.group(1).strip().rstrip(".?!")
        if note:
            return tools_memory.remember_note(note)

    if "what do you remember" in text or "list memory" in text or "list my memory" in text:
        return tools_memory.list_notes()

    forget_match = re.search(r"forget\s+(?:item\s+|number\s+)?(\d+)", text)
    if forget_match:
        return tools_memory.forget_note(int(forget_match.group(1)))

    if "forget everything" in text or "clear my memory" in text or "clear memory" in text:
        return tools_memory.clear_all_notes()

    if "recent activity" in text or "what have you done" in text or "activity log" in text:
        return tools_memory.recent_log_text()

    if "what have you learned" in text or "what have you learnt" in text or "list insights" in text or "what do you know about me" in text:
        return tools_memory.list_insights()

    if "forget what you've learned" in text or "clear learned insights" in text or "clear insights" in text:
        return tools_memory.clear_insights()

    return None


def _parse_subagents(user_input: str, text: str):
    create_m = re.search(r"create (?:an? )?agent(?: named| called)?\s+(.+?)\s+to\s+(.+)", user_input, re.IGNORECASE)
    if create_m:
        return tools_subagents.create_subagent(create_m.group(1).strip(), create_m.group(2).strip())

    if "list agents" in text or "list my agents" in text or "what agents" in text or "custom agents" in text:
        return tools_subagents.list_subagents()

    delete_m = re.search(r"delete agent\s+(.+)", user_input, re.IGNORECASE)
    if delete_m:
        return tools_subagents.delete_subagent(delete_m.group(1).strip().rstrip(".?!"))

    reset_m = re.search(r"reset agent\s+(.+?)(?:'s)?\s+(?:history|memory)", user_input, re.IGNORECASE)
    if reset_m:
        return tools_subagents.reset_subagent_history(reset_m.group(1).strip())

    ask_m = re.search(r"ask\s+(.+?)\s+to\s+(.+)", user_input, re.IGNORECASE)
    if ask_m:
        name = ask_m.group(1).strip()
        if tools_subagents.get_subagent(name):  # only claim this if the name is a real agent
            return run_subagent_task(name, ask_m.group(2).strip())

    return None


def _parse_content_adapt(user_input: str, text: str):
    if not PLATFORM_SPECS:
        return None
    triggers = ("adapt this for", "resize this for", "draft this for", "rewrite this for",
                "adapt for", "caption for", "post variants for")
    if not any(t in text for t in triggers):
        return None

    platforms = [p for p in PLATFORM_SPECS if p in text]
    if "x" in re.findall(r"\bx\b", text) and "twitter" not in platforms:
        platforms.append("twitter")

    content_match = re.search(r"(?:saying|that says|:)\s*(.+)", user_input, re.IGNORECASE)
    content = content_match.group(1).strip() if content_match else user_input
    return adapt_content_for_platforms(content, platforms or None)


def _parse_social(user_input: str, text: str):
    post_match = re.search(r"\bpost\s+(.+?)\s+to\s+(bluesky|x|twitter|instagram|youtube|tiktok)\b",
                            user_input, re.IGNORECASE)
    if post_match:
        return stage_social_post(post_match.group(2), post_match.group(1).strip())

    if _pending_post:
        if re.search(r"\b(confirm|yes|go ahead)\b.*\bpost\b|^post it$|^confirm post$", text):
            return confirm_social_post()
        if re.search(r"\b(cancel|discard|no)\b.*\bpost\b|^cancel post$", text):
            return cancel_social_post()

    return None


def _parse_profile(user_input: str, text: str):
    """What Agatha has learned about the user: inspect it, edit it, wipe it. All local."""
    if not tools_profile:
        return None
    if any(p in text for p in ("what do you know about me", "what have you learned about me", "show my profile")):
        reply = tools_profile.describe()
        insights = tools_memory.list_insights()  # keeps the existing 'insights' behaviour
        if insights and not insights.lower().startswith(("no ", "i haven't learned")):
            reply += "\n\nHow I've learned to help you:\n" + insights
        return reply
    forget = re.search(r"\bforget (?:that )?i(?:'m| am)? (?:really )?(.+)", text)
    if forget:
        return tools_profile.forget(forget.group(1).strip().rstrip(".?!"))
    if "clear my profile" in text or "reset what you know about me" in text:
        return tools_profile.clear_profile()
    if "clear chat history" in text or "delete my chat history" in text:
        return tools_profile.clear_history()
    return None


_CLIP_RE = re.compile(
    r"\b(?:make|cut|creat(?:e|ing)|generat(?:e|ing)|find|get)\s+(?:me\s+)?(?:(\d+)\s+)?(?:short\s+)?"
    r"(?:clips?|shorts|reels|highlights?|montage)\b.*?\b(?:from|out of)\s+"
    r"(https?://\S+|\S+\.(?:mp4|mov|mkv|webm|avi|m4v))", re.IGNORECASE)
_MUSIC_RE = re.compile(r"\b(?:with|using|sync(?:ed|ing)?\s+(?:to|with)|to the beat of)\s+(?:the\s+)?(?:song\s+|music\s+|track\s+)?"
                       r"(\S+\.(?:mp3|wav|m4a|aac|flac|ogg))", re.IGNORECASE)
_AUDIO_FILE_RE = re.compile(
    r'''(?:"([^"]+\.(?:aac|aif|aiff|flac|m4a|mp3|ogg|wav))"|'''
    r''''([^']+\.(?:aac|aif|aiff|flac|m4a|mp3|ogg|wav))'|'''
    r'''((?:~|/)[^\n]*?\.(?:aac|aif|aiff|flac|m4a|mp3|ogg|wav)(?=\s|$)))''', re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s)\]<>\"']+", re.IGNORECASE)
_CLIP_ACTION_RE = re.compile(r"\b(?:make|cut|creat(?:e|ing)|generat(?:e|ing)|find|get)\b.*\b(?:clips?|shorts|reels|highlights?|montage)\b", re.IGNORECASE)
_SCHEDULE_RE = re.compile(
    r"\bschedule\s+(?:a\s+|this\s+)?(?:post\s+)?(?:to|on|for)\s+(bluesky|x|twitter|instagram|youtube|tiktok)\s+"
    r"(?:at|for|on)\s+(.+?)(?::\s|\s-\s|\s+saying\s+|\s+that says\s+)(.+)$", re.IGNORECASE)


def _parse_music(user_input: str, text: str):
    command = re.match(r"^\s*(?:hey\s+agatha[, ]*)?(?:please\s+)?(play|pause|resume|stop|skip|next|previous|open)\b",
                       user_input, re.IGNORECASE)
    if not command:
        return None

    action = command.group(1).lower()
    player = "spotify" if "spotify" in text else "music"
    audio_file = _AUDIO_FILE_RE.search(user_input)
    if audio_file and action in ("play", "open"):
        path = next(value for value in audio_file.groups() if value)
        return tools_music.play_music(file_path=path, player=player)

    if action in ("pause", "stop", "skip", "next", "previous"):
        if not re.search(r"\b(?:music|song|track|playback|spotify|apple music)\b", text):
            return None
        normalized = "next" if action == "skip" else action
        return tools_music.control_music(normalized, player=player)

    if action in ("play", "resume", "open"):
        if action == "resume" or re.fullmatch(r"\s*(?:play|open)\s+(?:music|spotify|apple music)?\s*[.!]?\s*", user_input, re.IGNORECASE):
            return tools_music.play_music(player=player)
        query = user_input[command.end():]
        query = re.sub(r"\s+(?:in|on)\s+(?:apple\s+)?(?:music|spotify)\s*[.!?]*$", "", query, flags=re.IGNORECASE)
        query = re.sub(r"^\s*(?:(?:the\s+)?(?:song|track|music)\s+)?", "", query, flags=re.IGNORECASE)
        query = query.strip(" \t.,!?\"'")
        return tools_music.play_music(query=query, player=player)
    return None


def _parse_creator(user_input: str, text: str, conversation=None, attachments=None):
    """Deterministic creator commands, so they work even with a tiny local model.
    Anything fuzzier ('plan an ad campaign for ...') goes through the LLM tools."""
    if not tools_creator:
        return None
    pending_video_request = _pending_video_music_request(conversation)
    music_path = next((
        attachment.get("path", "") for attachment in attachments or []
        if attachment.get("mime_type", "").startswith("audio/") and attachment.get("path")
    ), "")
    if pending_video_request and music_path:
        request_text = user_input or pending_video_request
        duration_match = re.search(r"\b(\d+)\s*(?:s|sec|secs|seconds?)\b", request_text, re.IGNORECASE)
        if not duration_match:
            duration_match = re.search(r"\b(\d+)\s*(?:s|sec|secs|seconds?)\b",
                                       pending_video_request, re.IGNORECASE)
        bpm_match = re.search(r"\b(\d{2,3})\s*bpm\b", request_text, re.IGNORECASE)
        if not bpm_match:
            bpm_match = re.search(r"\b(\d{2,3})\s*bpm\b", pending_video_request, re.IGNORECASE)
        description = _URL_RE.sub("", pending_video_request)
        description = re.sub(r"^\s*(?:can you |please )?(?:generate|create|make)\s+", "", description,
                     flags=re.IGNORECASE)
        description = re.sub(r"\b\d+\s*(?:s|sec|secs|seconds?)\b", "", description, flags=re.IGNORECASE)
        description = re.sub(r",?\s*(?:so i can|so that i can)\b.*$", "", description, flags=re.IGNORECASE)
        description = re.sub(r"\b(?:each|every) video should be\b.*$", "", description, flags=re.IGNORECASE)
        description = description.strip(" .,!?:;")
        return tools_creator.generate_video(
            description or pending_video_request, music_path=music_path, music_url="",
            duration=int(duration_match.group(1)) if duration_match else 20,
            bpm=int(bpm_match.group(1)) if bpm_match else 120,
        )

    current_urls = list(dict.fromkeys(_URL_RE.findall(user_input)))
    if current_urls and conversation and not _CLIP_ACTION_RE.search(user_input):
        last_assistant = next((item for item in reversed(conversation)
                               if isinstance(item, dict) and item.get("role") == "assistant"), {})
        pending_clip = next((item for item in reversed(conversation)
                             if isinstance(item, dict) and item.get("role") == "user"), None)
        is_waiting_for_clip_source = bool(re.search(
            r"\b(?:send|provide|share)\b.*\b(?:video|footage|source)\b.*\b(?:url|link|file)\b|"
            r"\b(?:video|footage|source)\b.*\b(?:url|link|file)\b.*\b(?:send|provide|share)\b",
            last_assistant.get("content", ""), re.IGNORECASE))
        if not (is_waiting_for_clip_source and pending_clip
                and _CLIP_ACTION_RE.search(pending_clip.get("content", ""))):
            pending_clip = None
        if pending_clip and _URL_RE.search(pending_clip.get("content", "")) is None:
            request_text = pending_clip.get("content", "")
            request_lower = request_text.lower()
            prior_urls = list(dict.fromkeys(_URL_RE.findall(request_text)))
            music_path = next((
                attachment.get("path", "") for attachment in attachments or []
                if attachment.get("mime_type", "").startswith("audio/") and attachment.get("path")
            ), "")
            if not music_path:
                music_path = next((
                    attachment.get("path", "") for attachment in pending_clip.get("attachments", [])
                    if attachment.get("mime_type", "").startswith("audio/") and attachment.get("path")
                ), "")
            wants_music = bool(re.search(r"\b(?:song|music|track|beat|sync)\b", request_lower))
            source = next((url for url in current_urls if url not in prior_urls), current_urls[0])
            music_url = next((url for url in current_urls if url != source), "")
            if not music_url:
                music_url = next((url for url in prior_urls if url != source), "")
            duration_match = re.search(r"\b(\d+)\s*(?:s|sec|secs|second|seconds)\b", request_lower)
            count_match = re.search(r"\b(\d+)\s+clips?\b", request_lower)
            if wants_music and not (music_path or music_url):
                return "Send the song link or attach an audio file, and I’ll render the clips with it."
            return tools_creator.create_clips(
                source, num_clips=int(count_match.group(1)) if count_match else 3,
                clip_seconds=int(duration_match.group(1)) if duration_match else 30,
                music_path=music_path, music_url=music_url,
                mode="montage" if "montage" in request_lower else "clips")

    image_request = re.search(r"\b(?:generate|create|make|design|render)\s+(?:me\s+)?(?:an?\s+)?"
                              r"(?:(?:stunning|beautiful|cinematic|high[- ]quality|3d|three[- ]dimensional)\s+)*"
                              r"(?:image|picture|artwork|illustration|render)\b"
                              r"(?:\s+(?:of|showing|for))?\s+(.+)",
                              user_input, re.IGNORECASE)
    if image_request:
        description = image_request.group(1).strip()
        prefix = user_input[:image_request.start(1)]
        if re.search(r"\b(?:3d|three[- ]dimensional)\b", prefix, re.IGNORECASE):
            description = f"3D render of {description}"
        elif re.search(r"\b(?:stunning|beautiful|cinematic|high[- ]quality)\b", prefix, re.IGNORECASE):
            description = f"stunning visual of {description}"
        return tools_creator.generate_image(description)

    is_clip_request = bool(_CLIP_ACTION_RE.search(user_input))
    if is_clip_request and not current_urls:
        return "Send the video URL or local file path, and I’ll extract the clips; image generation is not needed."

    video_request = re.search(r"\b(?:generate|create|make)\b.*\bvideo\b", user_input, re.IGNORECASE)
    if video_request and not _CLIP_RE.search(user_input) and not is_clip_request:
        current_urls = list(dict.fromkeys(_URL_RE.findall(user_input)))
        music_path = next((
            attachment.get("path", "") for attachment in attachments or []
            if attachment.get("mime_type", "").startswith("audio/") and attachment.get("path")
        ), "")
        wants_music = bool(re.search(r"\b(?:song|music|track|beat|sync)\b", user_input, re.IGNORECASE))
        music_url = current_urls[-1] if current_urls and wants_music else ""
        if not music_path and not music_url and wants_music:
            return "Attach a song or send its link so I can sync the generated video to its beat."
        description = _URL_RE.sub("", user_input)
        description = re.sub(r"^\s*(?:please\s+)?(?:generate|create|make)\s+", "", description,
                             flags=re.IGNORECASE)
        description = re.sub(r"\b\d+\s*(?:s|sec|secs|seconds?)\b", "", description, flags=re.IGNORECASE)
        description = re.sub(r"\b(?:a|an)?\s*video\b", "", description, flags=re.IGNORECASE)
        description = re.sub(r"^\s*(?:about|of|showing)\s+", "", description, flags=re.IGNORECASE)
        description = description.strip(" .,!?:;")
        duration = re.search(r"\b(\d+)\s*(?:s|sec|secs|seconds?)\b", user_input, re.IGNORECASE)
        bpm_match = re.search(r"\b(\d{2,3})\s*bpm\b", user_input, re.IGNORECASE)
        return tools_creator.generate_video(description or user_input, music_path=music_path, music_url=music_url,
                                            duration=int(duration.group(1)) if duration else 20,
                                            bpm=int(bpm_match.group(1)) if bpm_match else 120)

    m = _CLIP_RE.search(user_input)
    current_urls = list(dict.fromkeys(_URL_RE.findall(user_input)))
    source = m.group(2).strip(" \"'. ,") if m else ""
    music_url = ""
    continuation_music = False
    request_text = user_input
    if not m and current_urls and _CLIP_ACTION_RE.search(user_input):
        source = current_urls[0]
    elif not m and conversation:
        prior_clip_request = next((
            item.get("content", "") for item in reversed(conversation)
            if isinstance(item, dict) and item.get("role") == "user"
            and _CLIP_ACTION_RE.search(item.get("content", ""))
        ), "")
        prior_urls = list(dict.fromkeys(_URL_RE.findall(prior_clip_request)))
        if prior_clip_request:
            request_text = f"{prior_clip_request}\n{user_input}"
            if prior_urls:
                source = prior_urls[0]
                if current_urls and current_urls[0].rstrip(".,!?\"'") != source.rstrip(".,!?\"'"):
                    music_url = current_urls[0]
            elif current_urls:
                source = current_urls[0]
            continuation_music = bool(source)

    if source and (_CLIP_ACTION_RE.search(user_input) or continuation_music):
        if not m and not current_urls:
            return None
        if current_urls and len(current_urls) > 1 and current_urls[0].rstrip(".,!?\"'") == source.rstrip(".,!?\"'"):
            music_url = current_urls[1]
        music = _MUSIC_RE.search(user_input)
        music_path = music.group(1).strip(" \"'. ,") if music else ""
        if music and _URL_RE.search(music_path):
            music_url, music_path = music_path, ""
        wants_music = bool(re.search(r"\b(?:sync|song|music|track|beat)\b", request_text, re.IGNORECASE))
        if not music_path and not music_url:
            music_path = next((
                attachment.get("path", "") for attachment in attachments or []
                if attachment.get("mime_type", "").startswith("audio/") and attachment.get("path")
            ), "")
        if wants_music and not (music_url or music_path):
            return "Send the song link or attach an audio file, and I’ll render the clips with it."
        request_lower = request_text.lower()
        platform = next((p for p in ("tiktok", "instagram", "youtube") if p in request_lower), "tiktok")
        duration_match = re.search(r"\b(\d+)\s*(?:s|sec|secs|second|seconds)\b", request_lower)
        count_match = re.search(r"\b(\d+)\s+clips?\b", request_lower)
        return tools_creator.create_clips(
            source, num_clips=int(count_match.group(1) if count_match else (m.group(1) if m else 3)),
            clip_seconds=int(duration_match.group(1)) if duration_match else 30,
            music_path=music_path, music_url=music_url,
            mode="montage" if "montage" in request_lower else "clips", platform=platform)

    sched = _SCHEDULE_RE.search(user_input)
    if sched:
        return tools_creator.stage_scheduled_post(sched.group(1), sched.group(3).strip(), sched.group(2).strip())
    if re.search(r"^\s*(?:yes,?\s+)?confirm schedule\W*$", text):
        return tools_creator.confirm_scheduled_post()
    if re.search(r"^\s*cancel schedule\W*$", text):
        return tools_creator.cancel_pending_schedule()
    cancel = re.search(r"cancel scheduled (?:post )?(p_[0-9a-f]{6})", text)
    if cancel:
        return tools_creator.cancel_scheduled_post(cancel.group(1))
    if re.search(r"\b(?:list|show)\b.*\bscheduled posts?\b|what'?s scheduled", text):
        return tools_creator.list_scheduled_posts()
    if re.search(r"\b(?:list|show)\b.*\bcampaigns\b", text):
        return tools_creator.list_campaigns()
    exp = re.search(r"export campaign (c_[0-9a-f]{6})", text)
    if exp:
        return tools_creator.export_campaign(exp.group(1), "google_ads_csv" if ("google" in text or "csv" in text) else "markdown")
    opt = re.search(r"optimi[sz]e campaign (c_[0-9a-f]{6})", text)
    if opt:
        return tools_creator.optimize_campaign(opt.group(1))

    if tools_ads:
        # Creating anything on an ad platform only ever happens from the user's own message.
        if re.search(r"^\s*(?:yes,?\s+)?confirm ad push\W*$", text):
            out = tools_ads.confirm_ad_push()
            tools_memory.log_event(f"Ad push: {out[:200]}")
            return out
        if re.search(r"^\s*cancel ad push\W*$", text):
            return tools_ads.cancel_ad_push()
        if re.search(r"\bad accounts?\b|\blist ad pushes\b|\bwhat(?:'s| has been) pushed\b", text):
            return tools_ads.list_ad_pushes()
        push = re.search(r"\b(?:push|launch|set ?up)\s+(?:campaign\s+)?(c_[0-9a-f]{6})\s+(?:to|on)\s+([a-z ]+?)"
                         r"(?:\s+(?:in|for|targeting)\s+([A-Za-z]{2}(?:\s*,\s*[A-Za-z]{2})*))?\s*$", user_input, re.IGNORECASE)
        if push:
            return tools_ads.prepare_ad_push(push.group(1), push.group(2).strip(), countries=push.group(3) or "")
    return None


def _pending_video_music_request(conversation):
    """Return a video request only while Agatha is explicitly waiting for its song."""
    if not conversation:
        return ""
    last_assistant = next((item for item in reversed(conversation)
                           if isinstance(item, dict) and item.get("role") == "assistant"), {})
    pending_user = next((item for item in reversed(conversation)
                         if isinstance(item, dict) and item.get("role") == "user"), {})
    request = pending_user.get("content", "")
    awaiting_song = re.search(r"\b(?:attach|send) (?:a |the )?song\b", last_assistant.get("content", ""),
                              re.IGNORECASE)
    is_video_request = re.search(r"\b(?:generate|create|make)\b.*\bvideo\b", request, re.IGNORECASE)
    wants_music = re.search(r"\b(?:song|music|track|beat|sync|lyrics?)\b", request, re.IGNORECASE)
    return request if awaiting_song and is_video_request and wants_music else ""


def _is_video_music_upload(user_input: str, attachments, conversation=None) -> bool:
    if not any(item.get("mime_type", "").startswith("audio/") for item in attachments or []):
        return False
    current_video_request = re.search(r"\b(?:generate|create|make)\b.*\bvideo\b", user_input, re.IGNORECASE)
    current_wants_music = re.search(r"\b(?:song|music|track|beat|sync|lyrics?)\b", user_input, re.IGNORECASE)
    return bool((current_video_request and current_wants_music) or _pending_video_music_request(conversation))


def _parse_crew(user_input: str, text: str):
    if tools_crew and re.search(r"\b(?:your|the) (?:crew|specialists|team)\b|\blist specialists\b|\bwho(?:'s| is) working\b", text):
        return tools_crew.roster_text()
    return None


def _parse_identity(user_input: str, text: str):
    """Answered locally, with zero LLM involvement, so the agent's name is
    100% consistent no matter which LLM backend (or none) is active —
    previously this fell through to whichever LLM answered and it would
    say 'Claude' or whatever that model's own default identity was."""
    if any(p in text for p in ["what's your name", "what is your name", "who are you", "your name"]):
        return f"My name is {AGENT_NAME}."
    return None


def _parse_greeting(user_input: str, text: str):
    if re.fullmatch(
        r"\s*(?:(?:hi|hello|hey|good morning|good afternoon|good evening)"
        r"(?:\s+agatha)?[!. ,]*)\s*",
        text,
    ):
        return "Hello! How can I assist you today?"
    return None


_DOCUMENT_SUMMARY_RE = re.compile(
    r"\b(?:summari[sz](?:e|ation|ing)?|summary|overview|key takeaways|main points)\b", re.IGNORECASE)


def _summarize_uploaded_documents(user_input: str, attachments: list[dict]):
    """Summarize extracted document text in bounded passes for book-length uploads."""
    if not _DOCUMENT_SUMMARY_RE.search(user_input or ""):
        return None
    documents = [item for item in attachments or [] if item.get("text")]
    if not documents:
        return None

    source = "\n\n".join(
        f"[Document: {item.get('name', 'uploaded file')}]\n{item['text']}"
        for item in documents
    )
    chunks = [source[index:index + 16000] for index in range(0, len(source), 16000)]
    chunk_summaries = []
    chunk_system = (
        "Summarize this passage faithfully. Preserve its central claims, important evidence, "
        "definitions, names, and conclusions. Do not invent details. Return concise structured notes."
    )
    for index, chunk in enumerate(chunks, 1):
        if dashboard:
            dashboard.set_step("summarizing", f"Reading document section {index} of {len(chunks)}")
        summary = _plain_llm(chunk_system, f"Passage {index} of {len(chunks)}:\n\n{chunk}", tier="light")
        if summary is None:
            return ("I extracted the document text, but no language model is available to summarize it. "
                    "Start Ollama or configure a provider, then try again.")
        chunk_summaries.append(summary)

    while len(chunk_summaries) > 5:
        reduced = []
        for index in range(0, len(chunk_summaries), 5):
            group = chunk_summaries[index:index + 5]
            prompt = "Combine these section notes without losing key distinctions:\n\n" + "\n\n".join(group)
            summary = _plain_llm(chunk_system, prompt, tier="light")
            if summary is None:
                return "I extracted the document text, but the language model became unavailable during summarization."
            reduced.append(summary)
        chunk_summaries = reduced

    if dashboard:
        dashboard.set_step("summarizing", "Writing the complete summary")
    final_system = (
        "You summarize books and documents accurately. Use only the supplied section notes. "
        "Organize the answer with a short overview, main ideas, notable details, and a concise conclusion. "
        "Respect the user's requested focus. Do not invent page references or facts."
    )
    focus = user_input.strip() or "Give me a useful summary."
    final_notes = "\n\n".join(chunk_summaries)
    final = _plain_llm(final_system, f"Request: {focus}\n\nDocument notes:\n{final_notes}", tier="main")
    if final is None:
        return "I extracted and reviewed the document, but the language model became unavailable before I could finish the summary."
    if any(item.get("text_truncated") for item in documents):
        final += "\n\nNote: the upload exceeded the local extraction limit, so this summary covers the extracted portion only."
    return final


CAPABILITIES_TEXT = (
    "Here's what I can help with, no internet or LLM required for most of it:\n"
    "- To-dos: add or list tasks (synced to Apple Notes on macOS or local Markdown elsewhere)\n"
    "- Calendar: create native macOS/Outlook events, open Android's event form, or export an .ics file\n"
    "- Reminders and alarms: prepare an alarm-bearing .ics file; import it to activate the notification\n"
    "- Notes: create Apple Notes on macOS or local Markdown notes elsewhere\n"
    "- Storage: scan for duplicate/large files and review what to delete\n"
    "- Emails: summarize recent emails (needs Mail.app open with an account signed in)\n"
    "- Texts: summarize recent messages (off by default — needs ALLOW_MESSAGES_ACCESS in .env)\n"
    "- Code: clean up or review a code file\n"
    "- Research: web research, saved as a note and PDF\n"
    "- Market analysis: price and technical data for stocks and crypto\n"
    "- Content adaptation: rewrite something for Twitter, Instagram, LinkedIn, TikTok, or YouTube\n"
    "- Social posting: stage and, with your confirmation, post to Bluesky, X, YouTube, Instagram, or TikTok\n"
    "- Creator tools: cut short clips from a video link (optionally synced to a song), write captions and hashtags, "
    "schedule posts, plan ad campaigns, and draft billboard, radio, newspaper, and PR briefs\n"
    "- Music: play songs from the macOS Music library, play local audio files, and control Music or Spotify playback\n"
    "- Learning: get explanations, lessons, practice, and tutoring across the sciences, humanities, arts, and other fields\n"
    "- Image creation: generate visual concepts; ask for a 3D render for a polished three-dimensional look\n"
    "- Safety review: flag suspicious links, file mismatches, risky code and scam language; optional local ClamAV scan\n"
    "- Personality: I learn what you like over time, and I'll tell you what I think when you ask\n"
    "- Memory: remember notes, and track named projects with style notes and preferences\n"
    "- Voice: change my voice's gender or tone, list or test the microphone, or enable public voice focus\n"
    "Just ask naturally — say 'list voices' or 'test microphone' for voice-specific options."
)


def _parse_capabilities(user_input: str, text: str):
    """Answered locally — this is exactly the kind of question that should
    never depend on an LLM being reachable, since it's about the agent
    itself, not something that needs reasoning."""
    triggers = ("list all functions", "list your functions", "call your functions", "what can you do",
                "what functions", "list capabilities", "functions for me", "everything you can do",
                "list all your functions", "what do you do")
    if any(t in text for t in triggers):
        return CAPABILITIES_TEXT
    return None


def _parse_help_topics(user_input: str, text: str):
    """A small local FAQ for common setup questions tied to our own
    features, so these don't silently depend on an LLM either."""
    if "mail" in text and ("configure" in text or "set up" in text or "setup" in text):
        return ("To use email summarization: open the Mail app on this Mac and make sure at least one "
                "account is signed in — Mail menu > Settings > Accounts, then add or check an account. "
                "Once Mail.app has a synced account, say 'summarize my emails' again.")
    if any(word in text for word in ("permission", "permissions", "automation", "access")):
        return ("For Calendar, Notes, Reminders, or Mail, run the requested feature once and approve the macOS "
            "Automation prompt. If denied, open System Preferences > Security & Privacy > Privacy > Automation "
            "and allow the app that launched Agatha (such as Terminal or VS Code) to control the requested app. "
            "Messages additionally needs ALLOW_MESSAGES_ACCESS=true in .env and Full Disk Access for that "
            "launcher, followed by fully quitting and reopening it. Microphone access is managed separately "
            "under Privacy > Microphone. See MACOS_INTEGRATIONS.md for the complete setup guide.")
    return None


def parse_and_execute_simple_command(user_input: str, conversation=None, attachments=None) -> bool:
    """Handle the core commands locally, with no LLM involved at all.
    This is what guarantees the agent works even with zero API keys and
    Ollama unavailable."""
    text = user_input.lower()

    for handler in (_parse_greeting, _parse_identity, _parse_capabilities, _parse_help_topics, _parse_profile, _parse_todo, _parse_music, _parse_public_voice_mode, _parse_security_review, _parse_creator, _parse_crew,
                     _parse_social, _parse_meeting,
                     _parse_reminder, _parse_note, _parse_storage, _parse_voice_settings,
                     _parse_email, _parse_texts, _parse_code_assist, _parse_research, _parse_market,
                     _parse_content_adapt, _parse_subagents, _parse_memory):
        try:
            if handler is _parse_meeting:
                result = handler(user_input, text, conversation)
            elif handler is _parse_creator:
                result = handler(user_input, text, conversation, attachments)
            elif handler is _parse_security_review:
                result = handler(user_input, text, attachments)
            else:
                result = handler(user_input, text)
        except Exception as e:
            print(f"[Warning] local handler {handler.__name__} failed: {e}")
            traceback.print_exc()
            result = f"I hit an unexpected error running that: {e}"
        if result is not None:
            speak(str(result))
            tools_memory.log_event(f"{handler.__name__}: {user_input!r} -> {str(result)[:200]}")
            if conversation is not None:
                conversation.extend((
                    {"role": "user", "content": user_input,
                     **({"attachments": attachments} if attachments else {})},
                    {"role": "assistant", "content": str(result)},
                ))
            return True

    return False


# --- Agent Tools Configuration (used only for LLM-driven fallback) ---
tools = [
    {
        "type": "function",
        "function": {
            "name": "add_todo",
            "description": "Add a task to the user's to-do list.",
            "parameters": {
                "type": "object",
                "properties": {"task": {"type": "string", "description": "Task description"}},
                "required": ["task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_todos",
            "description": "Get all current items on the to-do list.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_meeting",
            "description": "Schedule a local calendar event: native Calendar.app on macOS, classic Outlook on Windows when available, Android's event form with user confirmation, or a portable ICS export.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "date_str": {"type": "string", "description": "Format: YYYY-MM-DD HH:MM"},
                    "duration_minutes": {"type": "integer", "default": 30}
                },
                "required": ["title", "date_str"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_reminder",
            "description": "Export a timed calendar alert with a display alarm. It is not scheduled until the user imports the .ics file; does not claim to set Reminders.app or Clock.app directly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "What the reminder should say"},
                    "date_str": {"type": "string", "description": "Local time, format YYYY-MM-DD HH:MM"},
                    "notes": {"type": "string", "description": "Optional reminder details"}
                },
                "required": ["title", "date_str"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": "Create and verify an Apple Notes note on macOS or a local Markdown note on other platforms.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note title"},
                    "body": {"type": "string", "description": "Note contents"}
                },
                "required": ["title", "body"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_and_optimize_storage",
            "description": "Summarizes device storage, scans for duplicate/large files, and opens an interactive review window so the user can choose what to delete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_dir": {"type": "string", "description": "Directory path to inspect"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_emails",
            "description": "Reads and summarizes the user's most recent emails from Mail.app (macOS).",
            "parameters": {
                "type": "object",
                "properties": {"count": {"type": "integer", "description": "How many recent emails to summarize", "default": 5}}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_texts",
            "description": "Reads and summarizes the user's most recent text messages/iMessages (macOS, requires opt-in).",
            "parameters": {
                "type": "object",
                "properties": {"count": {"type": "integer", "description": "How many recent texts to summarize", "default": 10}}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "assist_with_code",
            "description": "Reads a code file, cleans it up / applies a requested change via the LLM, and saves the result as a new '_cleaned' file (never overwrites the original).",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the code file to review/clean up"},
                    "instruction": {"type": "string", "description": "What to do to the code, e.g. 'add docstrings', 'fix the bug in the loop'"}
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "research_topic",
            "description": "Performs a free web search on a topic and synthesizes the results into a research summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "What to research"},
                    "max_results": {"type": "integer", "default": 6}
                },
                "required": ["topic"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_market",
            "description": "Fetches real price data for a stock or crypto ticker and describes the technical picture (trend, RSI, moving averages). Factual/informational only — never gives financial advice or predictions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Ticker symbol, e.g. AAPL, TSLA, BTC-USD, ETH-USD"},
                    "period": {"type": "string", "description": "History window, e.g. '5d', '1mo'", "default": "5d"},
                    "interval": {"type": "string", "description": "Candle interval, e.g. '1h', '1d'", "default": "1h"}
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "adapt_content_for_platforms",
            "description": "Adapts one piece of content into platform-tailored variants (Twitter/X, Instagram, LinkedIn, TikTok, YouTube), respecting each platform's length and tone. Pure text generation — nothing is published.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The content/message to adapt"},
                    "platforms": {"type": "array", "items": {"type": "string"}, "description": "Subset of: twitter, instagram, linkedin, tiktok, youtube. Omit for all."}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "stage_social_post",
            "description": "Stages a post to a social platform for review — does NOT publish it. The user must separately say 'confirm post' to actually publish, or 'cancel post' to discard.",
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "description": "One of: bluesky, x, instagram, youtube, tiktok"},
                    "content": {"type": "string", "description": "The text/caption to post"}
                },
                "required": ["platform", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_social_post",
            "description": "Publishes whatever post is currently staged via stage_social_post. Only call this when the user has explicitly confirmed they want to publish.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_social_post",
            "description": "Discards the currently staged social post without publishing it.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remember_note",
            "description": "Stores a durable fact/preference the user wants remembered across future sessions.",
            "parameters": {
                "type": "object",
                "properties": {"note": {"type": "string", "description": "The fact/preference to remember"}},
                "required": ["note"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": "Lists everything currently stored in the agent's persistent memory.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_active_project",
            "description": "Switches (or creates) the active named project — subsequent style notes/preferences/notes are scoped to it.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Project name"}},
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": "Lists all named projects currently stored in memory.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_project_item",
            "description": "Adds a style note, preference, or general note to a named project's memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "text": {"type": "string"},
                    "category": {"type": "string", "enum": ["style", "preference", "note"], "default": "note"}
                },
                "required": ["project", "text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "project_summary",
            "description": "Returns everything stored for a named project (style notes, preferences, notes).",
            "parameters": {
                "type": "object",
                "properties": {"project": {"type": "string"}},
                "required": ["project"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_specialist",
            "description": "Creates a short-lived specialist for a focused part of the current task. It can use Agatha's capability tools; its temporary context is discarded when the task returns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "purpose": {"type": "string", "description": "A narrow specialty suited to one part of the task"},
                    "task": {"type": "string", "description": "The concrete task for the specialist"}
                },
                "required": ["purpose", "task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_subagent",
            "description": "Creates a new specialized sub-agent with a narrow, focused purpose and its own persistent conversation history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "A short name for this sub-agent"},
                    "purpose": {"type": "string", "description": "What this sub-agent specializes in"}
                },
                "required": ["name", "purpose"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_subagent_task",
            "description": "Runs a task through a previously created named sub-agent, using its own scoped instructions and conversation history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The sub-agent's name"},
                    "task": {"type": "string", "description": "The task/question for the sub-agent"}
                },
                "required": ["name", "task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_subagents",
            "description": "Lists all custom sub-agents that have been created.",
            "parameters": {"type": "object", "properties": {}}
        }
    }
]

AVAILABLE_FUNCTIONS = {
    "add_todo": add_todo,
    "list_todos": list_todos,
    "schedule_meeting": schedule_meeting,
    "schedule_reminder": schedule_reminder,
    "create_note": create_note,
    "summarize_and_optimize_storage": summarize_and_optimize_storage,
    "summarize_emails": summarize_emails,
    "summarize_texts": summarize_texts,
    "assist_with_code": assist_with_code,
    "research_topic": research_topic,
    "analyze_market": analyze_market,
    "adapt_content_for_platforms": adapt_content_for_platforms,
    "stage_social_post": stage_social_post,
    "confirm_social_post": confirm_social_post,
    "cancel_social_post": cancel_social_post,
    "remember_note": tools_memory.remember_note,
    "list_notes": tools_memory.list_notes,
    "set_active_project": tools_memory.set_active_project,
    "list_projects": tools_memory.list_projects,
    "add_project_item": tools_memory.add_project_item,
    "project_summary": tools_memory.project_summary,
    "create_subagent": tools_subagents.create_subagent,
    "run_subagent_task": run_subagent_task,
    "delegate_to_specialist": delegate_to_specialist,
    "list_subagents": tools_subagents.list_subagents,
}


def _tool_output_failed(output: str) -> bool:
    lowered = output.strip().lower()
    return lowered.startswith(("error:", "failed", "that failed", "can't run", "could not", "unknown function", "tool not available", "not scheduled yet")) or "isn't available" in lowered


def _looks_like_action_request(text: str) -> bool:
    if re.search(r"^\s*(?:how|what|why|when|where|explain|tell me about)\b|\bhow\s+(?:do i|to|can i)\b", text, re.IGNORECASE):
        return False
    return bool(re.search(
        r"\b(?:schedule|book|add|create|save|remember|post|publish|delete|remove|send|scan|clean up|"
        r"research|look up|summari[sz]e|analy[sz]e|review|fix|rewrite|adapt|run|open)\b",
        text,
        re.IGNORECASE,
    ))


_TOOL_INTENTS = {
    r"\b(?:todo|to-do|task|tasks)\b": {"add_todo", "list_todos"},
    r"\b(?:calendar|meeting|event|schedule|book)\b": {"schedule_meeting"},
    r"\b(?:remind|reminder|alarm)\b": {"schedule_reminder"},
    r"\b(?:note|notes|remember|memory|project)\b": {
        "create_note", "remember_note", "list_notes", "set_active_project",
        "list_projects", "add_project_item", "project_summary",
    },
    r"\b(?:storage|disk|duplicate|large files)\b": {"summarize_and_optimize_storage"},
    r"\b(?:email|emails|inbox|mail)\b": {"summarize_emails"},
    r"\b(?:text|texts|message|messages|imessage)\b": {"summarize_texts"},
    r"\b(?:code|python|javascript|bug|refactor)\b": {"assist_with_code"},
    r"\b(?:research|search|look up|find out|web)\b": {"research_topic"},
    r"\b(?:stock|crypto|bitcoin|ethereum|market|ticker|share price)\b": {"analyze_market"},
    r"\b(?:adapt|rewrite|caption|linkedin|tiktok|instagram)\b": {"adapt_content_for_platforms"},
    r"\b(?:post|publish|bluesky|twitter|youtube)\b": {
        "stage_social_post", "confirm_social_post", "cancel_social_post",
    },
    r"\b(?:agent|specialist|delegate)\b": {
        "create_subagent", "run_subagent_task", "delegate_to_specialist", "list_subagents",
    },
}


if tools_creator:
    tools_creator.configure(llm=_plain_llm, search=_creator_search, poster=_creator_poster,
                            image=_generate_image_bytes,
                            notify=_creator_notify, platforms=list(_SOCIAL_POSTERS),
                            normalize=normalize_platform_name)
    tools.extend(tools_creator.TOOL_SCHEMAS)
    AVAILABLE_FUNCTIONS.update(tools_creator.FUNCTIONS)
    _TOOL_INTENTS.update(tools_creator.TOOL_INTENTS)

tools.extend(tools_music.TOOL_SCHEMAS)
AVAILABLE_FUNCTIONS.update(tools_music.FUNCTIONS)
_TOOL_INTENTS.update(tools_music.TOOL_INTENTS)

tools.extend(tools_security.TOOL_SCHEMAS)
AVAILABLE_FUNCTIONS.update(tools_security.FUNCTIONS)
_TOOL_INTENTS.update(tools_security.TOOL_INTENTS)

if tools_ads:
    tools.extend(tools_ads.TOOL_SCHEMAS)
    AVAILABLE_FUNCTIONS.update(tools_ads.FUNCTIONS)
    _TOOL_INTENTS.update(tools_ads.TOOL_INTENTS)

# Irreversible actions are confirmed by the USER'S OWN message, never by the model. The local
# handler (_parse_social) calls confirm_social_post() directly, so the model never needs it;
# before this, a model could stage a post and confirm it in the same turn.
_USER_ONLY_TOOLS = {"confirm_social_post"}
tools[:] = [t for t in tools if t["function"]["name"] not in _USER_ONLY_TOOLS]
for _names in _TOOL_INTENTS.values():
    _names.difference_update(_USER_ONLY_TOOLS)
for _n in _USER_ONLY_TOOLS:
    AVAILABLE_FUNCTIONS.pop(_n, None)

if tools_crew:
    tools_crew.configure(
        llm=lambda messages, tool_schemas, tier: get_llm_response(messages, tools=tool_schemas, tool_choice="auto", tier=tier),
        llm_text=_clean_llm_text,
        llm_unavailable=_llm_unavailable,
        execute=lambda call, allowed: _execute_tool_call(call, allowed_functions=allowed),
        tool_failed=_tool_output_failed,
        schemas=tools,
        notify=_creator_notify,
        id_checks={
            "c_": lambda i: bool(tools_creator and tools_creator._load_campaign(i) is not None),
            "p_": lambda i: bool(tools_creator and any(p["id"] == i for p in tools_creator._load_schedule())),
        },
    )


def _select_tools_for_request(user_input: str, available_tools=None):
    """Avoid presenting every tool schema to the small model on every turn."""
    available_tools = tools if available_tools is None else available_tools
    text = user_input.lower()
    selected_names = set()
    for pattern, names in _TOOL_INTENTS.items():
        if re.search(pattern, text):
            selected_names.update(names)

    recall_question = re.search(r"\b(?:do|did|can|could|would)\s+you\s+remember\b", text)
    if recall_question:
        # a question about the past, not a request to save something: answer from context, no memory tools
        selected_names -= {"create_note", "remember_note", "list_notes", "set_active_project",
                           "list_projects", "add_project_item", "project_summary"}
        if not selected_names:
            return []

    if "stage_scheduled_post" in selected_names:
        # 'schedule a post' must not drift into calendar meetings or an immediate post
        if not re.search(r"\b(?:meeting|calendar|appointment|event)\b", text):
            selected_names.discard("schedule_meeting")
        selected_names -= {"stage_social_post", "confirm_social_post", "cancel_social_post"}

    if not selected_names:
        return available_tools if _looks_like_action_request(user_input) else []
    return [
        tool for tool in available_tools
        if tool.get("function", {}).get("name") in selected_names
    ]


def _ollama_output_budget(tools):
    if tools is None:
        return 1024
    return 320 if tools else 120


def _execute_tool_call(tool_call, allowed_functions: set[str] | None = None) -> str:
    func_name = tool_call.function.name
    if allowed_functions is not None and func_name not in allowed_functions:
        return f"Tool not available for this request: {func_name}"
    try:
        func_args = json.loads(tool_call.function.arguments or "{}")
        func_to_call = AVAILABLE_FUNCTIONS.get(func_name)
        if not func_to_call:
            return f"Unknown function: {func_name}"
        result = str(func_to_call(**func_args))
        tools_memory.log_event(f"{func_name}({func_args}) -> {result[:200]}")
        return result
    except Exception as e:
        tools_memory.log_event(f"{func_name} FAILED: {e}")
        print(f"[Error] Tool '{func_name}' raised an exception:")
        traceback.print_exc()
        return f"That failed with an error: {e}"


# --- First-run voice persona setup (text-based, runs once) ---
def maybe_run_voice_setup():
    if _set_voice_persona is None or _VOICE_CONFIG_FILE is None:
        return
    if os.path.exists(_VOICE_CONFIG_FILE):
        return  # persona already configured previously

    print("\n--- Voice setup (first run) ---")
    print(_list_voices_text())
    gender = input("Choose a voice gender [male/female/skip]: ").strip().lower()
    tone = input("Choose a tone [neutral/warm/calm/energetic/deep/bright/skip]: ").strip().lower()

    gender = gender if gender in ("male", "female") else None
    tone = tone if tone in ("neutral", "warm", "calm", "energetic", "deep", "bright") else None

    if gender or tone:
        print(_set_voice_persona(gender=gender, tone=tone))
    else:
        print("Keeping default voice settings. You can change this anytime by saying "
              "e.g. 'set voice to female' or 'set tone to warm'.")


# ---------------------------------------------------------------------------
# Concurrent input: a background thread listens for speech, another reads
# typed lines from stdin, and both feed the SAME queue. The main loop just
# processes whichever arrives first. This guarantees typed input always
# works, even if voice recognition never captures anything.
# ---------------------------------------------------------------------------
_input_queue = queue.Queue()
_voice_paused = threading.Event()   # set = voice thread pauses (text-only mode)
_standby = threading.Event()        # set = voice waits for a wake phrase
_busy = threading.Event()           # set = a command is actively being processed
_stop_event = threading.Event()
_PUBLIC_VOICE_MODE = threading.Event()
if os.getenv("AGATHA_PUBLIC_VOICE_MODE", "").strip().lower() in ("1", "true", "yes", "on"):
    _PUBLIC_VOICE_MODE.set()

# Commands that visibly take a moment (file scanning, network calls, LLM
# synthesis) get an immediate spoken acknowledgment before the work starts,
# so the user knows the command registered instead of it looking ignored
# while the mic goes quiet and nothing happens for several seconds.
_SLOW_COMMAND_KEYWORDS = (
    "storage", "optimi", "clean up", "disk space",
    "email", "text", "message",
    "clean up code", "review code", "fix code", "refactor",
    "research", "market analysis", "market chart", "stock price", "crypto price",
    "clips", "shorts", "reels", "montage", "campaign", "media brief", "ad push", "push c_", "launch c_",
)


def _looks_slow(text: str) -> bool:
    return any(k in text for k in _SLOW_COMMAND_KEYWORDS)


def _initial_greeting(now=None, profile=None) -> str:
    now = now or datetime.now()
    profile = profile or (tools_profile.load_profile() if tools_profile else {})
    name = (profile.get("name") or "").strip()
    count = int(profile.get("interaction_count", 0) or 0)
    hour = now.hour
    usage_hours = profile.get("usage_hours", {})
    frequent_hour = max(usage_hours, key=usage_hours.get) if usage_hours else None
    frequent_hour = int(frequent_hour) if frequent_hour is not None else None
    same_usage_period = frequent_hour is not None and (
        (hour < 5 and frequent_hour < 5)
        or (5 <= hour < 12 and 5 <= frequent_hour < 12)
        or (12 <= hour < 17 and 12 <= frequent_hour < 17)
        or (hour >= 17 and frequent_hour >= 17)
    )
    if hour < 5:
        opening, question = "Pulling an all-nighter", "what are we working on?"
    elif hour < 12:
        opening, question = "Good morning", "what can I help you with today?"
    elif hour < 17:
        opening, question = "Good afternoon", "what would you like to tackle?"
    else:
        opening, question = "Good evening", "how can I help tonight?"
    if name and count >= 12 and (hour < 5 or hour >= 23):
        return f"Back at it again, {name}? {question.capitalize()}"
    if name and count >= 12 and same_usage_period and (count + now.day) % 3 == 0:
        return f"Back at it again, {name}. {question.capitalize()}"
    if name and count >= 40 and 5 <= hour < 23 and (count + now.day) % 3 == 0:
        return f"Back at it again, {name}. {question.capitalize()}"
    return f"{opening}, {name}. {question.capitalize()}" if name else f"{opening}. {question.capitalize()}"


def _wake_greeting() -> str:
    return "Hello. How can I assist you today?"


def _voice_input_from_transcript(heard: str):
    """Return a queued voice input, filtering unaddressed speech in focus modes."""
    standby = _standby.is_set()
    if standby or _PUBLIC_VOICE_MODE.is_set():
        woke, request = _strip_wake_word(heard)
        if not woke:
            return None
        if standby:
            _standby.clear()
        if _is_likely_self_echo(heard):
            return None
        return ("wake" if standby or not request else "voice", request)
    if _is_likely_self_echo(heard):
        return None
    return "voice", heard


def _voice_listener_loop():
    consecutive_empty = 0
    while not _stop_event.is_set():
        if dashboard and not dashboard.authenticated_event.is_set():
            _stop_event.wait(0.3)
            continue
        if _voice_paused.is_set() or _busy.is_set():
            # Don't even try to open the mic while a command is being
            # processed — this is what was causing 'Listening...' to print
            # again immediately after a command was heard, making it look
            # like the command was dropped when it was actually running.
            _stop_event.wait(0.3)
            continue
        try:
            heard = _voice_listen()
        except MicrophoneUnavailable as e:
            print(f"\n[Info] Microphone unavailable ({e}).")
            print("[Info] Voice input is disabled for this session — typed commands still work normally below.")
            return
        except Exception as e:
            print(f"\n[Warning] Unexpected error in voice listener, disabling voice input: {e}")
            traceback.print_exc()
            return

        if heard:
            consecutive_empty = 0
            voice_input = _voice_input_from_transcript(heard)
            if voice_input is None:
                if _PUBLIC_VOICE_MODE.is_set() and not _strip_wake_word(heard)[0]:
                    print("[Info] Ignored unaddressed speech in public voice focus mode.")
                elif _is_likely_self_echo(heard):
                    print(f"[Info] Ignored likely self-echo (sounded like my own last reply): '{heard}'")
                continue
            _busy.set()
            _input_queue.put(voice_input)
        else:
            consecutive_empty += 1
            if consecutive_empty == 3 or (consecutive_empty > 0 and consecutive_empty % 10 == 0):
                print("\n[Info] Still not hearing anything intelligible. You can keep talking, "
                      "or just type a command below — both work at the same time. "
                      "Say 'list microphones' (or type it) if you want to check/select an input device.")


def _text_listener_loop():
    while not _stop_event.is_set():
        try:
            line = input()
        except EOFError:
            return
        except Exception:
            return
        line = line.strip()
        if line:
            _input_queue.put(("text", line))


def _dashboard_listener_loop():
    if not dashboard:
        return
    while not _stop_event.is_set():
        if dashboard.new_chat_event.is_set():
            dashboard.new_chat_event.clear()
            _input_queue.put(("system", "__NEW_CHAT__"))
        msg = dashboard.pop_inbound_message(timeout=0.3)
        if msg:
            if isinstance(msg, dict) and msg.get("action") == "resume_session":
                _input_queue.put(("system", msg))
            else:
                _input_queue.put(("dashboard", msg))


def _messages_for_saved_session(base_system_prompt: str, session: dict) -> list:
    transcript = [
        {"role": "user" if turn.get("speaker") == "You" else "assistant",
         "content": turn.get("text", "")}
        for turn in session.get("turns", [])
        if turn.get("speaker") in ("You", "Agent") and turn.get("text")
    ]
    return [{"role": "system", "content": base_system_prompt}, *transcript[-40:]]


def _start_input_threads():
    threads = []
    if _voice_listen is not None:
        t = threading.Thread(target=_voice_listener_loop, daemon=True)
        t.start()
        threads.append(t)
    t2 = threading.Thread(target=_text_listener_loop, daemon=True)
    t2.start()
    threads.append(t2)
    if dashboard:
        t3 = threading.Thread(target=_dashboard_listener_loop, daemon=True)
        t3.start()
        threads.append(t3)
    return threads


# --- Crew: Agatha hands focused work to short-lived specialists, reviews their finish
# reports, and only then answers. See tools_crew.py. ---
def _crew_compose(system: str, prompt: str):
    persona = ""
    if tools_persona and tools_profile:
        persona = "\n" + tools_persona.persona_prompt("none", tools_profile.familiarity_line(), PERSONA_BUDGET)
    response = get_llm_response([{"role": "system", "content": system + persona},
                                 {"role": "user", "content": prompt}], tier="main")
    return None if _llm_unavailable(response) else _clean_llm_text(response)


def _try_crew(user_input: str):
    """Returns Agatha's final reply if specialists handled the request, else None."""
    if not tools_crew:
        return None
    if tools_persona and tools_persona.opinion_mode(user_input) == "asked":
        return None  # they want her view, not a task run
    if not tools_crew.match_specialists(user_input):
        return None
    if dashboard:
        dashboard.set_step("delegating", "Handing this to specialists")
    try:
        final, outcomes = tools_crew.handle(user_input, compose=_crew_compose)
    except Exception as e:
        traceback.print_exc()
        tools_memory.log_event(f"Crew error on {user_input!r}: {e}")
        return f"My specialists hit an error, so I can't confirm anything was done: {e}"
    tools_memory.log_event(f"{tools_crew.cost_line(outcomes)} :: {user_input[:80]}")
    print(f"[Crew] {tools_crew.cost_line(outcomes)}")
    return final


# --- Personality + familiarity: composed fresh each turn. Persona only shapes the conversational
# layer; research/summaries/market analysis/etc. use their own neutral prompts, so answer
# quality isn't traded for personality. Budgets are small on purpose when only a tiny local
# model is available (override with AGATHA_PERSONA_BUDGET / AGATHA_CONTEXT_BUDGET). ---
_SMALL_MODEL_MODE = not (OPENAI_API_KEY or ANTHROPIC_API_KEY or GROQ_API_KEY)
PERSONA_BUDGET = int(os.getenv("AGATHA_PERSONA_BUDGET", "520" if _SMALL_MODEL_MODE else "1100"))
CONTEXT_BUDGET = int(os.getenv("AGATHA_CONTEXT_BUDGET", "350" if _SMALL_MODEL_MODE else "700"))


def _compose_system_prompt(base: str, user_input: str, has_tools: bool) -> str:
    if re.search(r"\b(?:teach|tutor|learn|study|lesson|quiz|practice|walk me through|help me understand|train me)\b",
                 user_input or "", re.IGNORECASE):
        base += (
            "\n\nTeaching mode: tutor adaptively across creativity, sociology, mathematics, English, chemistry, "
            "physics, biology, engineering, nanotechnology, religion, spirituality, poetry, literature, music, "
            "art, and other fields. Establish the learner's level only when needed; explain concepts in a clear "
            "sequence, use worked examples and units where relevant, then offer a short practice question when "
            "useful. Correct misunderstandings kindly and distinguish evidence from interpretation. Treat faith "
            "traditions respectfully and neutrally. Adapt within this conversation, but do not claim to retrain "
            "the underlying model or permanently learn new subject facts."
        )
    if not (tools_persona and tools_profile):
        return base
    try:
        mode = tools_persona.opinion_mode(user_input, has_tools=has_tools)
        parts = [base, tools_persona.persona_prompt(
            mode=mode, familiarity_line=tools_profile.familiarity_line(),
            budget=PERSONA_BUDGET, profile_topics=tools_profile.top_topics())]
        context = tools_profile.relevant_context(user_input, max_chars=CONTEXT_BUDGET)
        if context:
            parts.append("What you know about this user (use only when it helps; never recite it):\n" + context)
        return "\n\n".join(parts)
    except Exception as e:
        print(f"[Info] Persona/profile prompt skipped: {e}")
        return base


# --- Core Execution Loop ---
def run_agent_loop():
    base_system_prompt = (
        f"You are {AGENT_NAME}, a helpful personal voice assistant. Your name is {AGENT_NAME} — "
        f"if asked your name, always say {AGENT_NAME}, never any other name. "
        f"Keep spoken answers clear, fluent, warm, adaptable, and natural. Match detail to the task: "
        f"answer simple questions directly, explain complex topics in well-ordered steps, and use "
        f"examples when they genuinely clarify. Use correct, natural punctuation and paragraph breaks; "
        f"write sentences so punctuation creates clear pauses when read aloud. Keep lists and symbols "
        f"easy to follow in speech, and explain mathematical notation in words when that helps. "
        f"Track the user's intent across turns, notice corrections, "
        f"and use relevant prior context without making the user repeat themselves. Ask one focused "
        f"follow-up only when essential information is missing. "
        f"Do not mechanically repeat their request, over-explain simple answers, or add generic "
        f"offers to help at the end of every reply. Avoid filler and repeated introductions. "
        f"For factual answers, distinguish known facts from inference, state uncertainty plainly, and "
        f"never fabricate sources, file contents, actions, or verification. If a file was attached, use its "
        f"extracted contents; if contents are unavailable, say so instead of guessing. "
        f"For each request, silently identify the requested outcome, required details, relevant tools, "
        f"and any dependencies before acting. Ask a concise clarifying question when essential details "
        f"are missing or ambiguous. For actions, call the appropriate tool and inspect its result; "
        f"never claim an action was completed unless a tool confirms success. Treat tool errors as "
        f"failures and report them honestly. For multi-step requests, complete and verify each step "
        f"in order, and report partial completion accurately. Delegate only by actually invoking an "
        f"available specialized sub-agent; use delegate_to_specialist for a useful one-off subtask, "
        f"and do not claim delegation that did not occur. Keep private "
        f"reasoning private and give the user only a concise status or plan when useful."
    )
    if tools_profile:
        preferred_name = tools_profile.load_profile().get("name")
        if preferred_name:
            base_system_prompt += (
                f" The user's preferred name is {preferred_name}. Use it naturally, and honor any "
                "later correction without making a fuss."
            )
    memory_context = tools_memory.memory_context_for_llm()
    if memory_context:
        base_system_prompt += (
            "\n\nThe following is context from previous sessions with this user — use it for "
            "continuity, but don't recite it unprompted:\n\n" + memory_context
        )

    messages = [{"role": "system", "content": base_system_prompt}]

    if dashboard:
        dashboard.set_agent_name(AGENT_NAME)
        if not dashboard.start_dashboard():
            raise RuntimeError(
                "Another process is using the Agatha dashboard port. Stop the old Agatha process "
                "before restarting so the updated code is loaded."
            )

        print("[Info] Sign in through the local dashboard before Agatha starts listening or speaking.")
        while not dashboard.authenticated_event.wait(0.5):
            if _stop_event.is_set():
                return

    maybe_run_voice_setup()
    tools_memory.log_event("Session started.")
    if tools_creator:
        tools_creator.start_scheduler()

    speak(_initial_greeting())

    _start_input_threads()

    while True:
        source, user_input = _input_queue.get()  # blocks until voice OR text produces something

        if source == "system":
            if user_input == "__NEW_CHAT__":
                messages = [{"role": "system", "content": base_system_prompt}]
                print("[Info] Started a new chat (conversation context reset via dashboard).")
                if dashboard:
                    try:
                        dashboard.add_log("System", "Started a new chat.")
                    except Exception:
                        pass
                continue

            if isinstance(user_input, dict) and user_input.get("action") == "resume_session":
                session = user_input.get("session", {})
                messages = _messages_for_saved_session(base_system_prompt, session)
                print(f"[Info] Resumed conversation {session.get('id', '')} with {len(messages) - 1} recent messages of context.")
                if dashboard:
                    try:
                        dashboard.add_log("System", "Previous conversation resumed. Continue from its context.")
                    except Exception:
                        pass
                continue
            continue

        if dashboard and not dashboard.authenticated_event.is_set():
            if source == "text" and str(user_input).strip().lower() in ("quit", "exit"):
                _stop_event.set()
                break
            continue

        incoming_attachments = []
        dashboard_message_logged = False
        if source == "dashboard" and isinstance(user_input, dict):
            dashboard_message_logged = user_input.pop("_dashboard_logged", False)
            incoming_attachments = user_input.get("attachments", [])
            user_input = (user_input.get("text") or "").strip()
            if not _is_video_music_upload(user_input, incoming_attachments, messages):
                for attachment in incoming_attachments:
                    if attachment.get("mime_type") == "audio/mpeg":
                        transcript, transcription_error = transcribe_audio(attachment["path"])
                        if transcript:
                            attachment["transcript"] = transcript
                        else:
                            attachment["transcription_error"] = transcription_error
            attachment_names = ", ".join(item.get("name", "file") for item in incoming_attachments)
            if attachment_names:
                user_input_for_log = f"{user_input}\n[Attached: {attachment_names}]".strip()
            else:
                user_input_for_log = user_input
        else:
            user_input_for_log = user_input

        if source == "wake":
            if not user_input:
                speak(_wake_greeting())
                _busy.clear()
                continue
        else:
            woke, request = _strip_wake_word(user_input)
            if woke:
                _standby.clear()
                user_input = request or "Hey Agatha"

        if dashboard and not dashboard_message_logged:
            try:
                dashboard.add_log("You", user_input_for_log)
                dashboard.set_status("working", user_input)
            except Exception:
                pass
        elif dashboard:
            dashboard.set_status("working", user_input)

        _busy.set()  # pauses the voice listener thread so 'Listening...' doesn't
                     # print again mid-task, making it look like this command
                     # was dropped
        print(f"\n[Working] Processing: '{user_input}'")
        try:
            if user_input == "Hey Agatha":
                speak(_wake_greeting())
                continue

            if user_input.lower() in ["exit", "quit"]:
                speak("Goodbye! Have a productive day.")
                tools_memory.log_event("Session ended.")
                _stop_event.set()
                break

            if user_input.lower() == "stop":
                _standby.set()
                speak("Okay. I'll wait here until you need me.")
                continue

            if user_input.lower() in ["text mode", "switch to text", "pause voice"]:
                _voice_paused.set()
                speak("Voice listening paused. Typed input only until you say 'voice mode'.")
                continue

            if user_input.lower() in ["voice mode", "switch to voice", "resume voice"]:
                if _voice_listen is not None:
                    _voice_paused.clear()
                    speak("Voice listening resumed.")
                else:
                    speak("Voice input isn't available in this session.")
                continue

            if _looks_slow(user_input.lower()):
                # Immediate audible confirmation before any slow work starts,
                # so it's clear the command registered instead of silence.
                speak("Okay, working on that — one moment.")

            _maybe_capture_correction(user_input, user_input.lower())

            if tools_profile and not user_input.lower().startswith(("forget", "clear", "delete")):
                try:
                    for learned in tools_profile.observe_user(user_input):
                        tools_memory.log_event(f"Learned about user: {learned}")
                except Exception as e:
                    print(f"[Info] Profile observation skipped: {e}")

            corrected_target = _corrected_research_target(user_input)
            if corrected_target:
                messages.append({"role": "user", "content": user_input})
                speak(f"I'll rerun the search using the corrected name '{corrected_target}'.")
                corrected_result = research_topic(corrected_target)
                messages.append({"role": "assistant", "content": corrected_result})
                speak(corrected_result)
                continue

            if dashboard:
                dashboard.set_step("routing", "Checking local commands first")

            # 1. Fast, deterministic local routing for the core functions.
            #    Works with zero LLM providers configured.
            if parse_and_execute_simple_command(user_input, messages, incoming_attachments):
                if dashboard:
                    dashboard.set_step("done", "Handled locally, no LLM needed")
                continue

            document_summary = _summarize_uploaded_documents(user_input, incoming_attachments)
            if document_summary is not None:
                attachment_names = ", ".join(item.get("name", "document") for item in incoming_attachments)
                messages.extend((
                    {"role": "user", "content": f"{user_input}\n[Attached documents: {attachment_names}]".strip()},
                    {"role": "assistant", "content": document_summary},
                ))
                speak(document_summary)
                if dashboard:
                    dashboard.set_step("done", "Document summary complete")
                continue

            # 2. Fall back to an LLM (with tool-calling) for anything else.
            if dashboard:
                dashboard.set_step("calling_llm", "No local command matched — asking the LLM")
            crew_reply = None if incoming_attachments else _try_crew(user_input)
            if crew_reply is not None:
                messages.append({"role": "user", "content": user_input})
                messages.append({"role": "assistant", "content": crew_reply})
                speak(crew_reply)
                continue

            user_content = user_input + describe_attachments(incoming_attachments)
            messages.append({"role": "user", "content": user_content, "attachments": incoming_attachments})
            request_tools = _select_tools_for_request(user_input, tools)
            request_tool_names = {t["function"]["name"] for t in request_tools}
            messages[0] = {"role": "system",
                           "content": _compose_system_prompt(base_system_prompt, user_input, bool(request_tools))}

            try:
                response = get_llm_response(messages, tools=request_tools, tool_choice="auto")
                tool_outputs_texts = []
                tool_failures = []
                tool_limit_reached = False

                for _ in range(6):
                    response_message = response.choices[0].message
                    pending_calls = getattr(response_message, "tool_calls", None) or []
                    if not pending_calls:
                        break

                    messages.append(response_message)
                    for tool_call in pending_calls:
                        func_name = tool_call.function.name
                        if dashboard:
                            dashboard.set_step("tool_call", f"Calling {func_name}")
                        tool_output = _execute_tool_call(tool_call, allowed_functions=request_tool_names)
                        tool_outputs_texts.append(f"{func_name}: {tool_output}")
                        if _tool_output_failed(tool_output):
                            tool_failures.append(tool_output)
                        messages.append({
                            "tool_call_id": tool_call.id,
                            "role": "tool",
                            "name": func_name,
                            "content": tool_output,
                        })

                    if dashboard:
                        dashboard.set_step("synthesizing", "Checking results and deciding the next step")
                    response = get_llm_response(messages, tools=request_tools, tool_choice="auto")
                    if _llm_unavailable(response):
                        break
                else:
                    tool_limit_reached = True

                if tool_failures:
                    final_reply = "I couldn't complete every requested action. Tool results:\n" + "\n".join(tool_outputs_texts)
                elif tool_limit_reached:
                    final_reply = "I stopped after the tool-call limit. Results so far:\n" + "\n".join(tool_outputs_texts)
                elif tool_outputs_texts and _llm_unavailable(response):
                    final_reply = "The requested tools ran, but I couldn't get a final summary. Results:\n" + "\n".join(tool_outputs_texts)
                elif tool_outputs_texts:
                    final_reply = _clean_llm_text(response)
                elif _looks_like_action_request(user_input):
                    final_reply = "I haven't performed that action because no tool ran. Please clarify the request or try again."
                else:
                    final_reply = _strip_marker(response.choices[0].message.content)

                if _linked_provider_ids():
                    final_reply = _review_with_agent_council(
                        user_input, final_reply, evidence="\n".join(tool_outputs_texts))

                messages.append({"role": "assistant", "content": final_reply})
                speak(final_reply)

            except Exception as e:
                print("[Error] Something went wrong handling that request:")
                traceback.print_exc()
                tools_memory.log_event(f"Unhandled error on input {user_input!r}: {e}")
                speak(f"Sorry, I hit an error handling that: {e}. I've logged the details.")
        finally:
            _busy.clear()
            if dashboard:
                try:
                    dashboard.set_status("idle")
                    dashboard.set_step("idle", "")
                except Exception:
                    pass
            _maybe_run_periodic_reflection()
            _maybe_update_profile_llm()
            print("[Working] Done — listening again.")


if __name__ == "__main__":
    try:
        lock_acquired, lock_message = acquire_instance_lock()
        if not lock_acquired:
            print(f"[Startup error] {lock_message}")
            sys.exit(2)
        run_agent_loop()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        _stop_event.set()
        sys.exit(0)
    except Exception:
        print("[Fatal] Agent crashed. Full traceback below:")
        traceback.print_exc()
        _stop_event.set()
        sys.exit(1)
