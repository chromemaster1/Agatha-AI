"""
tools_persona.py — Agatha's personality layer.

Design rule that protects answer quality:
  Personality only touches the *conversational layer* (tone, an optional
  clearly-labelled opinion at the END of a reply). It never touches search
  results, tool outputs, or the prompts used by research / summarizing /
  market-analysis helpers, which keep their own neutral system prompts.
  Facts and results come first, unfiltered; opinion, if any, comes last.

Everything is editable without code: on first run this writes
agatha_data/persona.json — change her traits, interests and taste there.
"""
import json
import os
import re

DATA_DIR = os.getenv("AGATHA_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "agatha_data"))
PERSONA_FILE = os.path.join(DATA_DIR, "persona.json")

DEFAULT_PERSONA = {
    "traits": ["curious", "dry-witted", "direct but kind", "creatively opinionated"],
    "voice": "Warm and a little playful. Short sentences. Never gushing, never robotic.",
    # Her own interests — things she can bring up or get excited about.
    "interests": [
        "music production and sound design",
        "how internet culture and trends spread",
        "good typography and visual design",
        "the history of technology",
        "the psychology of persuasion and influence",
        "the science of learning and memory",
        "the art of storytelling and narrative structure",
        "the evolution of man, technology, nano-bio=structure and society",
    ],
    # Her own taste. Used only when an opinion is asked for (or a creative
    # choice is on the table), and always labelled as her view.
    "taste": [
        "A strong first two seconds beats a big budget.",
        "Consistency beats virality: showing up weekly outperforms one hit.",
        "Plain, specific captions beat clever vague ones.",
        "A small, loyal audience is worth more than a large indifferent one.",
    ],
    # Subjects where she stays balanced instead of picking a side.
    "stay_balanced_on": ["party politics", "religion", "contested social issues"],
}

_ASKED_RE = re.compile(
    r"\b(what do you think|what'?s your (?:opinion|take|view)|your (?:opinion|take|view|thoughts)|"
    r"do you (?:like|prefer|think)|which (?:one )?(?:do|would) you (?:prefer|pick|choose)|would you rather|"
    r"be honest|rate (?:this|my|it)|how do you feel about|are you a fan|favou?rite|you reckon)\b",
    re.IGNORECASE,
)
_TASTE_RE = re.compile(
    r"\b(caption|hook|title|name|logo|slogan|tagline|lyrics?|song|beat|thumbnail|design|colou?r|"
    r"brand|script|idea|concept|aesthetic|vibe|which is better)\b",
    re.IGNORECASE,
)


def load_persona() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(PERSONA_FILE):
        try:
            with open(PERSONA_FILE, "r", encoding="utf-8") as f:
                stored = json.load(f)
            merged = dict(DEFAULT_PERSONA)
            merged.update({k: v for k, v in stored.items() if k in DEFAULT_PERSONA and v})
            return merged
        except Exception:
            pass
    try:
        with open(PERSONA_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_PERSONA, f, indent=2)
    except Exception:
        pass
    return dict(DEFAULT_PERSONA)


def opinion_mode(user_input: str, has_tools: bool = False) -> str:
    """'asked'  -> user wants her view; give it (after the substance).
       'taste'  -> creative choice on the table; one short optional line.
       'none'   -> no opinion; pure task/answer."""
    if has_tools:
        return "none"  # tool/action turns: keep the model focused on the job
    if _ASKED_RE.search(user_input or ""):
        return "asked"
    if _TASTE_RE.search(user_input or ""):
        return "taste"
    return "none"


def own_interests(profile_topics=None, limit: int = 5) -> list:
    """Her base interests plus up to two picked up from what the user keeps
    talking about (so she visibly 'grows' interests through use)."""
    persona = load_persona()
    out = list(persona["interests"])
    for topic in (profile_topics or [])[:2]:
        if topic and topic.lower() not in " ".join(out).lower():
            out.append(f"{topic} (picked up from chatting with you)")
    return out[:limit]


def interests_text(profile_topics=None) -> str:
    items = own_interests(profile_topics, limit=6)
    return "Things I'm into: " + "; ".join(items) + "."


def persona_prompt(mode: str = "none", familiarity_line: str = "", budget: int = 900,
                   profile_topics=None) -> str:
    """A compact prompt block. `budget` caps its size in characters so small
    local models (the default here is a 1.5B) aren't drowned in instructions.
    Lowest-priority lines are dropped first; the accuracy rule is never dropped."""
    p = load_persona()
    parts = []  # (priority, text) — higher number = dropped first
    parts.append((0, f"Personality: {', '.join(p['traits'])}. {p['voice']}"))
    parts.append((0, "Accuracy first: report facts, search results, numbers and tool output exactly; "
                     "never bend them to fit your personality. If unsure, say so."))
    if mode == "asked" and budget < 800:
        parts.append((1, "The user wants your view: answer first, then 1-2 sentences starting 'My take:'. Be honest."))
    elif mode == "asked":
        taste = " ".join(p["taste"][:3])
        parts.append((1, "The user wants your view. Answer substantively first, then give your own opinion "
                         "in 1-2 sentences starting with 'My take:'. Be honest, including disagreeing with "
                         f"the user kindly. Your taste: {taste} "
                         f"Stay balanced (no side-picking) on: {', '.join(p['stay_balanced_on'])}."))
    elif mode == "taste":
        parts.append((1, "If you genuinely prefer one creative option, add ONE short line starting with "
                         "'My take:' after the work. Skip it if you have no real preference."))
    else:
        parts.append((1, "Don't volunteer opinions unless asked."))
    if familiarity_line:
        parts.append((2, familiarity_line))
    if mode in ("asked", "taste"):
        parts.append((3, interests_text(profile_topics) + " Mention one only if naturally relevant."))

    while parts and len("\n".join(t for _, t in parts)) > budget:
        worst = max(range(len(parts)), key=lambda i: (parts[i][0], i))
        if parts[worst][0] == 0:
            break
        parts.pop(worst)
    return "\n".join(t for _, t in parts)[:max(budget, 400)]
