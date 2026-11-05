"""
tools_profile.py — what Agatha learns about the user over time.

Three layers, all stored locally in agatha_data/:
  history.jsonl  every user/agent turn (rotated), used for "remember when..." recall
  profile.json   durable facts: likes, dislikes, preferences, goals, name
  familiarity    derived from how much you've talked; changes her tone, not her accuracy

Extraction is two-speed:
  * observe_user()      cheap regex on EVERY user message, no LLM call
  * extract_with_llm()  occasional batch pass over recent messages (call every ~10 turns)

Privacy: facts touching health, money, credentials, politics, religion, or
sexuality are never auto-saved. Everything is inspectable ("what do you know
about me") and removable ("forget that I like jazz", "clear my profile").
"""
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime

DATA_DIR = os.getenv("AGATHA_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "agatha_data"))
PROFILE_FILE = os.path.join(DATA_DIR, "profile.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.jsonl")
_lock = threading.RLock()

MAX_HISTORY_BYTES = 1_500_000
KEEP_HISTORY_LINES = 3000
VALID_KINDS = {"like", "dislike", "preference", "goal", "fact"}

_SENSITIVE_RE = re.compile(
    r"\b(health|diagnos\w*|medic\w*|therap\w*|depress\w*|anxiety|illness|disease|cancer|pregnan\w*|"
    r"salary|income|debt|loan|bank\w*|credit card|password|passcode|\bpin\b|ssn|social security|"
    r"religio\w*|church|mosque|politic\w*|vote|voting|party|sexual\w*|divorce|lawsuit|arrest\w*|"
    r"diabet\w*|asthma|adhd|autis\w*|bipolar|epilep\w*|allerg\w*|surgery|disorder|disabilit\w*|condition|"
    r"hospital|sick|gay|lesbian|bisexual|transgender|queer|immigra\w*|visa|citizenship|ethnic\w*|racial|mental)\b",
    re.IGNORECASE,
)
_STOP = set("""a an the and or but if so to of in on at for with from by is are was were be been it its this that
these those i me my mine you your we our they them he she his her as do does did have has had not no yes can could
would should will just really very about into over some any more most than then too also what which who how when
where why please hey hello thanks thank""".split())

_VERB_TOKENS = {"like", "love", "hate", "dislike", "enjoy", "prefer", "adore", "into", "fan", "favorite", "favourite"}
_NAME_BLOCK = {"later", "back", "tomorrow", "maybe", "when", "if", "now", "please", "soon", "again", "tonight",
               "crazy", "stupid", "lazy"}
_TRAILING_NOISE = re.compile(
    r"\s+(?:a lot|so much|too|lol|haha|though|honestly|right now|these days|ngl)\s*$|"
    r"\s+(?:because|but|and then|so that|since|which|cause|cuz)\b.*$|"
    r"\s+and\s+(?:i|i'm|i am|my|we)\b.*$", re.IGNORECASE)

_OBJ_GUARD = r"(?!that\b|it when\b|when\b|how\b|you\b|the way\b)"
_LIKE_RE = re.compile(
    r"\bi\s+(?:really\s+|absolutely\s+|totally\s+|genuinely\s+)?(?:like|love|enjoy|adore|prefer)\s+" + _OBJ_GUARD +
    r"(.{2,80}?)(?=[.,;!?\n]|$)", re.IGNORECASE)
_INTO_RE = re.compile(
    r"\bi(?:'m|\s+am)\s+(?:really\s+)?(?:into|a\s+(?:big\s+)?fan\s+of)\s+" + _OBJ_GUARD +
    r"(.{2,80}?)(?=[.,;!?\n]|$)", re.IGNORECASE)
_DISLIKE_RE = re.compile(
    r"\bi (?:really |absolutely )?(?:hate|dislike|can'?t stand|cannot stand|don'?t like|do not like|"
    r"(?:am|'m) not a fan of)\s+(?!that\b|it when\b|when\b|how\b|you\b|the way\b)(.{2,80}?)(?=[.,;!?\n]|$)",
    re.IGNORECASE)
_FAV_RE = re.compile(r"\bmy favou?rite ([a-z][a-z ]{0,20}?) (?:is|are)\s+(.{2,60}?)(?=[.,;!?\n]|$)", re.IGNORECASE)
_NAME_RE = re.compile(r"\b(?:call me|my name is|i go by)\s+([A-Za-z][\w'-]{1,24}(?:\s[A-Z][\w'-]{1,24})?)")
_INSTRUCT_RE = re.compile(
    r"\b(?:from now on|going forward|i want you to|i'd like you to|please always)\s+(.{4,100}?)(?=[.!?\n]|$)",
    re.IGNORECASE)


# ----------------------------------------------------------------- storage --
def _now() -> float:
    return time.time()


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _blank_profile() -> dict:
    return {"name": None, "interaction_count": 0, "first_seen": _now(), "last_seen": _now(),
            "last_extract_ts": 0.0, "facts": [], "usage_hours": {}}


def load_profile() -> dict:
    with _lock:
        _ensure_dir()
        try:
            with open(PROFILE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            base = _blank_profile()
            base.update(data)
            return base
        except Exception:
            return _blank_profile()


def _save_profile(profile: dict):
    with _lock:
        _ensure_dir()
        tmp = PROFILE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2, ensure_ascii=False)
        os.replace(tmp, PROFILE_FILE)


def set_name(name: str):
    """Set the user's preferred name, including an explicit correction."""
    name = " ".join((name or "").split())
    if not name or len(name) > 80:
        return False
    with _lock:
        profile = load_profile()
        profile["name"] = name
        _save_profile(profile)
    return True


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _tokens(s: str) -> set:
    words = (w for w in re.findall(r"[a-z0-9']+", (s or "").lower()) if len(w) > 2 and w not in _STOP)
    return {w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w for w in words}


# ----------------------------------------------------------------- history --
def log_turn(role: str, text: str):
    """role: 'user' or 'agent'."""
    text = (text or "").strip()
    if not text:
        return
    with _lock:
        _ensure_dir()
        line = json.dumps({"ts": _now(), "role": role, "text": text[:2000]}, ensure_ascii=False)
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        try:
            if os.path.getsize(HISTORY_FILE) > MAX_HISTORY_BYTES:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()[-KEEP_HISTORY_LINES:]
                with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                    f.writelines(lines)
        except OSError:
            pass


def _read_history(limit: int = 2000) -> list:
    with _lock:
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
        except OSError:
            return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def clear_history() -> str:
    with _lock:
        try:
            os.remove(HISTORY_FILE)
        except OSError:
            pass
    return "Chat history cleared. I'll keep the preferences in your profile unless you clear that too."


# ------------------------------------------------------------------- facts --
def _is_sensitive(text: str) -> bool:
    return bool(_SENSITIVE_RE.search(text or ""))


def _clean_obj(obj: str) -> str:
    obj = _TRAILING_NOISE.sub("", obj.strip().strip("\"'“”"))
    obj = re.sub(r"^(?:to\s+)?", "", obj).strip(" -")
    return obj if 2 <= len(obj) <= 70 else ""


def _add_fact(profile: dict, kind: str, text: str, obj: str = "", topic: str = "", source: str = "rule") -> bool:
    """Returns True if a NEW fact was added (False if it reinforced an existing one)."""
    if kind not in VALID_KINDS or not text or _is_sensitive(text):
        return False
    key = _norm(text)
    facts = profile["facts"]
    if kind in ("like", "dislike") and obj:
        opposite = "dislike" if kind == "like" else "like"
        # the user changed their mind: drop the opposite statement about the same thing
        facts[:] = [f for f in facts if not (f["kind"] == opposite and _norm(f.get("obj", "")) == _norm(obj))]
    for f in facts:
        if _norm(f["text"]) == key:
            f["count"] = f.get("count", 1) + 1
            f["last"] = _now()
            return False
    facts.append({"id": uuid.uuid4().hex[:6], "kind": kind, "text": text, "obj": obj, "topic": topic or obj,
                  "count": 1, "first": _now(), "last": _now(), "source": source})
    if len(facts) > 200:  # keep the strongest/most recent
        facts.sort(key=lambda f: (f.get("count", 1), f.get("last", 0)), reverse=True)
        del facts[150:]
    return True


def observe_user(text: str) -> list:
    """Cheap per-message pass. Logs the turn, bumps the interaction counter,
    and extracts explicit first-person statements. Returns newly learned facts."""
    text = (text or "").strip()
    if not text:
        return []
    log_turn("user", text)
    learned = []
    with _lock:
        profile = load_profile()
        profile["interaction_count"] += 1
        profile["last_seen"] = _now()
        usage_hours = profile.setdefault("usage_hours", {})
        hour_key = str(datetime.now().hour)
        usage_hours[hour_key] = int(usage_hours.get(hour_key, 0)) + 1
        if "?" not in text:  # questions aren't statements about the user
            for m in _FAV_RE.finditer(text):
                cat, obj = m.group(1).strip().lower(), _clean_obj(m.group(2))
                if obj and _add_fact(profile, "like", f"favorite {cat} is {obj}", obj=obj, topic=cat):
                    learned.append(f"favorite {cat} is {obj}")
            for m in list(_LIKE_RE.finditer(text)) + list(_INTO_RE.finditer(text)):
                obj = _clean_obj(m.group(1))
                if obj and _add_fact(profile, "like", f"likes {obj}", obj=obj):
                    learned.append(f"likes {obj}")
            for m in _DISLIKE_RE.finditer(text):
                obj = _clean_obj(m.group(1))
                if obj and _add_fact(profile, "dislike", f"dislikes {obj}", obj=obj):
                    learned.append(f"dislikes {obj}")
            for m in _INSTRUCT_RE.finditer(text):
                tail = m.group(1).strip()
                if _add_fact(profile, "preference", f"wants me to {tail}", obj=tail, topic="how I should behave"):
                    learned.append(f"wants me to {tail}")
        nm = _NAME_RE.search(text)
        if nm:
            candidate = nm.group(1).strip()
            if candidate.split()[0].lower() not in _NAME_BLOCK and not _is_sensitive(candidate):
                profile["name"] = candidate
        _save_profile(profile)
    return learned


def forget(phrase: str) -> str:
    phrase = (phrase or "").strip().lower()
    if not phrase:
        return "Tell me what to forget, e.g. 'forget that I like jazz'."
    with _lock:
        profile = load_profile()
        before = len(profile["facts"])
        tokens = _tokens(phrase) - _VERB_TOKENS
        profile["facts"] = [
            f for f in profile["facts"]
            if phrase not in f["text"].lower() and not (tokens and tokens <= _tokens(f["text"]))
        ]
        removed = before - len(profile["facts"])
        if profile.get("name") and phrase in profile["name"].lower():
            profile["name"] = None
            removed += 1
        _save_profile(profile)
    return f"Forgot {removed} thing(s) matching '{phrase}'." if removed else f"I don't have anything saved matching '{phrase}'."


def clear_profile() -> str:
    with _lock:
        old = load_profile()
        fresh = _blank_profile()
        fresh["interaction_count"] = old.get("interaction_count", 0)  # keep familiarity; wipe what I "know"
        fresh["first_seen"] = old.get("first_seen", _now())
        _save_profile(fresh)
    return "Cleared everything I'd learned about your preferences. We'll start fresh from here."


def describe() -> str:
    profile = load_profile()
    facts = sorted(profile["facts"], key=lambda f: (f.get("count", 1), f.get("last", 0)), reverse=True)
    if not facts and not profile.get("name"):
        return "I haven't picked up much about your preferences yet. Tell me what you like and I'll keep it in mind."
    groups = {"like": [], "dislike": [], "preference": [], "goal": [], "fact": []}
    for f in facts:
        groups[f["kind"]].append(f["text"])
    lines = []
    if profile.get("name"):
        lines.append(f"Name: {profile['name']}")
    for kind, label in (("like", "Likes"), ("dislike", "Dislikes"), ("preference", "How you want me to act"),
                        ("goal", "Goals"), ("fact", "Other")):
        if groups[kind]:
            lines.append(f"{label}: " + "; ".join(groups[kind][:12]))
    n = profile['interaction_count']
    lines.append(f"We've talked {n} time{'s' if n != 1 else ''}. Say 'forget that ...' to remove anything, "
                 f"or 'clear my profile' to wipe it.")
    return "\n".join(lines)


def top_topics(limit: int = 3) -> list:
    """Things the user keeps coming back to (liked ≥2 times) — feeds Agatha's picked-up interests."""
    likes = [f for f in load_profile()["facts"] if f["kind"] == "like" and f.get("count", 1) >= 2 and f.get("obj")]
    likes.sort(key=lambda f: f["count"], reverse=True)
    return [f["obj"] for f in likes[:limit]]


# -------------------------------------------------------------- familiarity --
def familiarity_line() -> str:
    profile = load_profile()
    n = profile.get("interaction_count", 0)
    if n < 8:
        return "You're still getting to know the user: friendly and clear, don't presume."
    if n < 60:
        return "You know the user a little: relaxed tone; use what you know about them when it helps."
    if n < 250:
        return "You know the user well: casual and concise, light humour, skip pleasantries."
    return "You've worked with the user a long time: shorthand, easy banter, direct honest feedback."


# ------------------------------------------------------------------- recall --
_PAST_CUE = re.compile(r"\b(remember when|last time|we talked|you said|i told you|i mentioned|as i said|earlier)\b",
                       re.IGNORECASE)
_RECOMMEND_CUE = re.compile(r"\b(recommend|suggest|ideas?|what should|something to|for me|surprise me|playlist|"
                            r"what do i like)\b", re.IGNORECASE)


def relevant_context(query: str, max_chars: int = 600) -> str:
    """A small, query-specific block for the system prompt. Only facts that
    matter to THIS message go in, so small models aren't flooded."""
    profile = load_profile()
    facts = profile["facts"]
    q = _tokens(query)
    lines = []
    if profile.get("name"):
        lines.append(f"User's name: {profile['name']}.")

    prefs = [f["text"] for f in facts if f["kind"] == "preference"][:3]
    if prefs:
        lines.append("Standing preferences: " + "; ".join(prefs) + ".")

    scored = []
    for f in facts:
        if f["kind"] == "preference":
            continue
        overlap = len(q & _tokens(f["text"]))
        if overlap:
            scored.append((overlap * 10 + f.get("count", 1), f))
    if _RECOMMEND_CUE.search(query or ""):
        for f in facts:
            if f["kind"] in ("like", "dislike") and all(f is not s[1] for s in scored):
                scored.append((f.get("count", 1), f))
    scored.sort(key=lambda s: s[0], reverse=True)
    rel = [f["text"] for _, f in scored[:5]]
    if rel:
        lines.append("Relevant about the user: " + "; ".join(rel) + ".")

    past = recall_history(query)
    if past:
        lines.append("Earlier the user said: " + " | ".join(f'"{p}"' for p in past))

    text = "\n".join(lines)
    return text[:max_chars]


def recall_history(query: str, k: int = 2, skip_recent: int = 8) -> list:
    """Old user messages that overlap strongly with the query. Only fires on an
    explicit 'remember when…' cue or a strong (≥3 word) topical match."""
    q = _tokens(query)
    needed = 2 if _PAST_CUE.search(query or "") else 3
    if len(q) < needed:
        return []
    hist = [h for h in _read_history() if h.get("role") == "user"]
    hist = hist[:-skip_recent] if len(hist) > skip_recent else []
    scored = []
    for h in hist:
        overlap = len(q & _tokens(h["text"]))
        if overlap >= needed:
            scored.append((overlap, h["ts"], h["text"]))
    scored.sort(reverse=True)
    out = []
    for _, ts, t in scored[:k]:
        when = datetime.fromtimestamp(ts).strftime("%b %d")
        out.append(f"{t[:140]} ({when})")
    return out


# ------------------------------------------------------- LLM-assisted pass --
def _extract_json_list(raw: str):
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def extract_with_llm(llm, min_new_turns: int = 4, max_new_facts: int = 3) -> int:
    """`llm(system, prompt) -> str | None`. Looks only at user messages since the
    last pass. Returns the number of facts added."""
    profile = load_profile()
    since = profile.get("last_extract_ts", 0.0)
    new_user = [h for h in _read_history(400) if h.get("role") == "user" and h["ts"] > since]
    if len(new_user) < min_new_turns:
        return 0
    batch = "\n".join(f"- {h['text'][:300]}" for h in new_user[-16:])
    raw = llm(
        "You extract durable personal preferences from a user's messages. Reply with ONLY a JSON list.",
        "Messages from one user:\n" + batch + "\n\n"
        "Return a JSON list (max 3 items) of objects {\"kind\": \"like|dislike|preference|goal|fact\", "
        "\"text\": \"short statement, e.g. 'likes lo-fi beats'\"}. Include ONLY things the user clearly said "
        "about themselves that will still be true next month. Exclude: health, money, passwords, relationships, "
        "politics, religion, and one-off tasks. If nothing qualifies, return [].")
    if raw is None:
        return 0  # LLM unavailable: try again next time, don't advance the pointer
    added = 0
    with _lock:
        profile = load_profile()
        for item in _extract_json_list(raw)[:max_new_facts]:
            if not isinstance(item, dict):
                continue
            kind, text = str(item.get("kind", "")).lower(), str(item.get("text", "")).strip()
            if kind in VALID_KINDS and 3 <= len(text) <= 90 and _add_fact(profile, kind, text, source="llm"):
                added += 1
        profile["last_extract_ts"] = _now()
        _save_profile(profile)
    return added
