"""
tools_crew.py — Agatha's crew of short-lived specialist sub-agents.

How a request flows
  1. match      which specialists does this request need?          (regex, free)
  2. plan       one sub-task each; dependent ones run in stages     (≤1 small LLM call, only if 2+ specialists)
  3. dispatch   independent tasks run in PARALLEL, each in an ephemeral specialist
  4. report     every specialist returns a structured Report
  5. review     Agatha checks each report before trusting it         (deterministic, free)
  6. conclude   one grounded final answer for the user

Why it's cheap
  * a specialist sees only its own 1-6 tool schemas, never the whole registry
  * no chat history, a ~300-character prompt, at most `max_steps` LLM calls
  * runs on the "light" model tier (see get_llm_response(tier=...))
  * a successful tool call ends the run: no extra "now summarise it" LLM call

Why it's ephemeral
  * a specialist exists only inside one call to _run_specialist(): no resident
    process, no history, no state. Worker threads live in a ThreadPoolExecutor
    that is torn down as soon as the batch finishes. Only the roster
    DEFINITIONS (below) persist.

Safety invariant
  No specialist can publish, spend, or launch anything. Their tools only
  prepare/stage; the user's own 'confirm ...' message is the only thing that
  executes an irreversible action (and those confirm functions are never
  exposed as tools).
"""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

# ------------------------------------------------------------------ wiring --
_deps = {
    "llm": None,              # (messages, tools, tier) -> response
    "llm_text": None,         # response -> str
    "llm_unavailable": None,  # response -> bool
    "execute": None,          # (tool_call, allowed:set) -> str
    "tool_failed": lambda out: False,
    "schemas": [],            # the full tool registry (we filter per specialist)
    "notify": print,
    "id_checks": {},          # {"c_": fn(id)->bool, "p_": fn(id)->bool}: prove claimed ids really exist
}


def configure(**kw):
    _deps.update({k: v for k, v in kw.items() if v is not None})


# ------------------------------------------------------------------ roster --
@dataclass(frozen=True)
class Specialist:
    key: str
    title: str
    blurb: str
    tools: tuple
    prompt: str
    triggers: str = ""
    max_steps: int = 2
    single_shot: bool = True   # stop after a successful tool call (no LLM summary pass)
    tier: str = "light"


ROSTER = {s.key: s for s in [
    Specialist(
        "researcher", "Researcher", "looks things up on the web and summarises what it finds",
        ("research_topic",), "Use research_topic for the topic asked about.",
        triggers=r"\b(?:research|look up|find out|competitors?|trends?)\b"),
    Specialist(
        "clip_editor", "Clip Editor", "cuts source footage or creates vertical image-based videos synced to a song",
        ("create_clips", "generate_video", "generate_image"),
        "Use create_clips when source footage is provided. Use generate_video when no video source is given; describe the visual concept and pass an attached song or music_url when available. Use generate_image for image-only requests. Never claim completion unless the tool returns verified downloadable artifact links.",
        triggers=r"\b(?:clips?|shorts?|reels?|highlights?|montage)\b", max_steps=1),
    Specialist(
        "copywriter", "Copywriter", "writes captions, hooks, hashtags and calls to action per platform",
        ("generate_captions", "adapt_content_for_platforms"),
        "Use generate_captions for new captions; adapt_content_for_platforms only to rewrite existing text.",
        triggers=r"\b(?:captions?|hashtags?|hooks?|adapt|rewrite)\b"),
    Specialist(
        "publisher", "Publisher", "stages or schedules posts for the user to confirm (never publishes)",
        ("stage_scheduled_post", "list_scheduled_posts", "cancel_scheduled_post", "stage_social_post", "cancel_social_post"),
        "Use stage_scheduled_post when a time is given, stage_social_post for an immediate post. Staging only.",
        triggers=r"\b(?:schedule|queue)\b.*\b(?:posts?|upload|video|content|reel|clip|short|tweet)\b|"
                 r"\b(?:scheduled posts?|post (?:this|it) (?:to|on)|publish)\b"),
    Specialist(
        "ads_strategist", "Ads Strategist",
        "plans ad campaigns (budget, audiences, copy, tests), logs results, optimises, and drafts billboard/radio/newspaper/PR briefs",
        ("plan_ad_campaign", "list_campaigns", "export_campaign", "log_campaign_results", "optimize_campaign",
         "build_media_brief"),
        "Use plan_ad_campaign for a new plan, optimize_campaign/log_campaign_results for results, "
        "build_media_brief for billboard/radio/newspaper/PR/influencer. Never invent budgets or ids.",
        triggers=r"\b(?:ads?|advert\w*|campaigns?|ad ?sets?|target audience|marketing plan|promot\w+|billboards?|"
                 r"radio|newspapers?|press release|media coverage|influencers?)\b"),
    Specialist(
        "ads_operator", "Ads Operator",
        "prepares a saved campaign draft for Google Ads, Meta (Instagram/WhatsApp) or TikTok; creates everything PAUSED after the user confirms",
        ("prepare_ad_push", "list_ad_pushes", "list_campaigns"),
        "Use prepare_ad_push with the campaign id and platform. It only previews; the user confirms separately.",
        triggers=r"\b(?:push|launch|upload|send|set ?up|create)\b.*\b(?:campaigns?|ads?|ad ?sets?)\b.*\b(?:google|meta|facebook|instagram|whatsapp|tiktok)\b|"
                 r"\b(?:google|meta|facebook|instagram|whatsapp|tiktok)\b.*\b(?:push|launch|set ?up)\b|"
                 r"\b(?:push|launch|set ?up)\b.*\bc_[0-9a-f]{6}\b|\bc_[0-9a-f]{6}\b.*\b(?:google|meta|facebook|instagram|whatsapp|tiktok)\b",
        max_steps=1),
]}

# who must finish before whom (only counted when both are in the plan)
DEPENDS = {
    "ads_strategist": {"researcher"},
    "copywriter": {"clip_editor", "ads_strategist"},
    "publisher": {"clip_editor", "copywriter"},
    "ads_operator": {"ads_strategist"},
}

_ACTION_RE = re.compile(
    r"\b(?:make|create|cut|generate|write|plan|build|draft|schedule|queue|launch|push|set ?up|export|optimi[sz]e|"
    r"log|list|show|stage|post|upload|find|research|prepare|run|get)\b", re.IGNORECASE)
_ID_RE = re.compile(r"\bc_[0-9a-f]{6}\b")
_active = set()
_active_lock = threading.Lock()
_group_locks = {}
_group_locks_guard = threading.Lock()


def active_specialists() -> list:
    with _active_lock:
        return sorted(_active)


def roster_text() -> str:
    lines = [f"- {s.title}: {s.blurb}" for s in ROSTER.values()]
    busy = active_specialists()
    lines.append("Working right now: " + (", ".join(ROSTER[k].title for k in busy) if busy
                                          else "nobody (specialists only exist while a task runs)"))
    return "My crew:\n" + "\n".join(lines)


# ------------------------------------------------------------------ routing --
def match_specialists(text: str) -> list:
    """Which specialists does this request need? Ordered by dependency stage."""
    text = text or ""
    if not _ACTION_RE.search(text):
        return []
    hit = [k for k, s in ROSTER.items() if s.triggers and k != "researcher" and re.search(s.triggers, text, re.IGNORECASE)]
    if "ads_operator" in hit and "ads_strategist" in hit and _ID_RE.search(text):
        hit.remove("ads_strategist")          # an existing campaign id: just push it
    elif "ads_operator" in hit and "ads_strategist" not in hit and not _ID_RE.search(text):
        hit.append("ads_strategist")          # no draft yet: plan first, then push
    if hit and "researcher" in ROSTER and re.search(ROSTER["researcher"].triggers, text, re.IGNORECASE):
        hit.append("researcher")              # research only joins a plan; it doesn't start one
    if "publisher" in hit and "ads_strategist" in hit and not re.search(r"\b(?:schedule|queue|post)\b", text, re.I):
        hit.remove("publisher")
    return sorted(set(hit), key=lambda k: (_stage_of(k, set(hit)), k))


def _stage_of(key: str, present: set) -> int:
    deps = DEPENDS.get(key, set()) & present
    return 1 + max((_stage_of(d, present) for d in deps), default=0)


@dataclass
class Task:
    key: str
    text: str
    stage: int = 1


def build_plan(request: str, keys: list) -> list:
    """One sub-task per specialist. A single specialist gets the request as-is.
    With several, a small LLM call splits the request, but only if it keeps every
    URL/path/number intact; otherwise each gets the full request."""
    present = set(keys)
    stages = {k: _stage_of(k, present) for k in keys}
    texts = {k: request for k in keys}
    if len(keys) > 1 and _deps["llm"]:
        listing = "\n".join(f"{k}: {ROSTER[k].blurb}" for k in keys)
        resp = _deps["llm"](
            [{"role": "system", "content": "You split a user's request into sub-tasks, one per specialist. Reply ONLY with a JSON object."},
             {"role": "user", "content": f"Specialists:\n{listing}\n\nRequest: {request}\n\n"
              'Return {"<specialist key>": "<self-contained sub-task>"} for each. Copy URLs, file paths, '
              "names and numbers exactly. Do not invent anything."}], None, "light")
        if not _deps["llm_unavailable"](resp):
            try:
                raw = re.sub(r"```(?:json)?", "", _deps["llm_text"](resp))
                data = json.loads(re.search(r"\{.*\}", raw, re.DOTALL).group(0))
                parts = {k: str(data[k]).strip() for k in keys if str(data.get(k, "")).strip()}
                protected = re.findall(r"https?://\S+|/[\w./~-]+\.\w{2,4}|\$?\d[\d,.]*", request)
                joined = " ".join(parts.values())
                if len(parts) == len(keys) and all(p.rstrip(".,") in joined for p in protected):
                    texts = parts
            except Exception:
                pass
    return [Task(k, texts[k], stages[k]) for k in keys]


# ------------------------------------------------------------------ reports --
@dataclass
class Report:
    specialist: str
    task: str
    status: str = "pending"          # ok | needs_input | failed
    text: str = ""
    steps: list = field(default_factory=list)   # [{tool, args, output, ok}]
    llm_calls: int = 0
    prompt_chars: int = 0
    seconds: float = 0.0
    attempt: int = 1
    notes: list = field(default_factory=list)


@dataclass
class Review:
    verdict: str                     # accept | retry | reject
    notes: list = field(default_factory=list)
    feedback: str = ""


@dataclass
class Outcome:
    report: Report
    review: Review


_SYS = ("You are the {title}, a short-lived specialist working for Agatha. Do ONLY the task you are given. {prompt} "
        "Take arguments from the task; never invent file paths, URLs, ids or numbers. If something essential is missing, "
        "ask ONE short question instead of calling a tool. You cannot publish, post, spend money or launch ads; you only "
        "prepare things for the user to confirm.")


def _chars(messages) -> int:
    total = 0
    for m in messages:
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        total += len(c or "")
    return total


def _run_specialist(spec: Specialist, task: str, feedback: str = "", attempt: int = 1) -> Report:
    rep = Report(spec.key, task, attempt=attempt)
    t0 = time.time()
    schemas = [s for s in _deps["schemas"] if s["function"]["name"] in spec.tools]
    allowed = {s["function"]["name"] for s in schemas}
    if not schemas:
        rep.status, rep.text = "failed", f"{spec.title}'s tools aren't loaded in this session."
        return rep
    user = task + (f"\n\nYour last attempt failed: {feedback[:300]}\nCall the right tool with corrected arguments." if feedback else "")
    messages = [{"role": "system", "content": _SYS.format(title=spec.title, prompt=spec.prompt)},
                {"role": "user", "content": user}]
    schema_chars = len(json.dumps(schemas))
    with _active_lock:
        _active.add(spec.key)
    try:
        for _ in range(spec.max_steps):
            resp = _deps["llm"](messages, schemas, spec.tier)
            rep.llm_calls += 1
            rep.prompt_chars += _chars(messages) + schema_chars
            if _deps["llm_unavailable"](resp):
                rep.status, rep.text = "failed", "No language model was reachable."
                return rep
            msg = resp.choices[0].message
            calls = getattr(msg, "tool_calls", None) or []
            if not calls:
                rep.text = (getattr(msg, "content", "") or "").strip()
                break
            messages.append(msg)
            batch_ok = True
            for c in calls:
                out = _deps["execute"](c, allowed)
                ok = not _deps["tool_failed"](out)
                batch_ok &= ok
                rep.steps.append({"tool": c.function.name, "args": (c.function.arguments or "")[:200], "output": out, "ok": ok})
                messages.append({"tool_call_id": c.id, "role": "tool", "name": c.function.name, "content": out[:1500]})
            if batch_ok and spec.single_shot:
                break
    except Exception as e:
        rep.status, rep.text = "failed", f"{spec.title} crashed: {e}"
        return rep
    finally:
        with _active_lock:
            _active.discard(spec.key)
        rep.seconds = round(time.time() - t0, 1)

    if rep.steps:
        last_batch_ok = rep.steps[-1]["ok"]
        if last_batch_ok:
            rep.status = "ok"
            if any(not s["ok"] for s in rep.steps):
                rep.notes.append("recovered after a failed first attempt")
            rep.text = "\n".join(s["output"].strip() for s in rep.steps if s["ok"])
        else:
            out = rep.steps[-1]["output"].strip()
            if "?" in out and out.lower().startswith("could not"):  # the tool is asking for a missing detail
                rep.status, rep.text = "needs_input", re.sub(r"^could not [^:]+:\s*", "", out, flags=re.IGNORECASE)
            else:
                rep.status, rep.text = "failed", out
    elif rep.text:
        rep.status = "needs_input" if "?" in rep.text else "failed"
        if rep.status == "failed":
            rep.notes.append("answered in words without running any tool")
    else:
        rep.status, rep.text = "failed", "No tool was run and no answer was given."
    return rep


# ------------------------------------------------------------------- review --
_PATH_RE = re.compile(r"(?<![\w:/])(/[\w ./~@+-]*?[\w-]+\.(?:mp4|mov|srt|md|csv|json|wav|mp3))\b")
_FIXABLE_RE = re.compile(r"required positional|unexpected keyword|missing \d+ required|invalid|not valid json|"
                         r"must be|expecting|is not a number|needs? to be", re.IGNORECASE)


def review(rep: Report) -> Review:
    """Agatha's check of a finish report. Deterministic: costs no tokens."""
    import os
    if rep.status == "needs_input":
        return Review("accept", ["asked the user for a missing detail"])
    if rep.status == "failed":
        if not rep.steps and rep.attempt == 1:
            return Review("retry", ["no tool was run"], "You must call one of your tools; do not answer in words.")
        if rep.steps and rep.attempt == 1 and _FIXABLE_RE.search(rep.text):
            return Review("retry", ["tool rejected the arguments"], rep.text)
        return Review("reject", [rep.text[:200] or "failed"])
    issues = []
    for s in rep.steps:
        if not s["ok"]:
            continue
        for p in _PATH_RE.findall(s["output"]):
            if not os.path.exists(p):
                issues.append(f"claims file {p} but it doesn't exist")
        for prefix, check in _deps["id_checks"].items():
            for ident in re.findall(rf"\b{re.escape(prefix)}[0-9a-f]{{6}}\b", s["output"]):
                try:
                    if not check(ident):
                        issues.append(f"mentions {ident} but no such record exists")
                except Exception:
                    pass
    if issues:
        return Review("reject", issues)
    return Review("accept", list(rep.notes))


def _group_lock(key: str) -> threading.Lock:
    with _group_locks_guard:
        return _group_locks.setdefault(key, threading.Lock())


def _run_with_review(task: Task) -> Outcome:
    spec = ROSTER[task.key]
    with _group_lock(task.key):  # two tasks for the same specialist queue up (e.g. two renders)
        rep = _run_specialist(spec, task.text)
        rv = review(rep)
        if rv.verdict == "retry":
            _deps["notify"](f"{spec.title}: retrying once ({'; '.join(rv.notes)}).")
            rep = _run_specialist(spec, task.text, feedback=rv.feedback, attempt=2)
            rv = review(rep)
    return Outcome(rep, rv)


# ----------------------------------------------------------------- dispatch --
def run_plan(plan: list, max_workers: int = 3, wait_seconds: int = 3600) -> list:
    """Run stage by stage; tasks inside a stage run in parallel."""
    outcomes = []
    for stage in sorted({t.stage for t in plan}):
        batch = [t for t in plan if t.stage == stage]
        context = _context_from(outcomes)
        if context:
            batch = [Task(t.key, t.text + "\n\nResults from earlier steps (use these, don't redo them):\n" + context, t.stage)
                     for t in batch]
        if stage > 1:
            _fill_campaign_id(batch, outcomes)
        if len(batch) == 1:
            outcomes.append(_run_with_review(batch[0]))
            continue
        _deps["notify"]("Working in parallel: " + ", ".join(ROSTER[t.key].title for t in batch))
        # the pool is a `with` block: its threads are gone the moment the batch is done
        with ThreadPoolExecutor(max_workers=min(max_workers, len(batch)), thread_name_prefix="crew") as pool:
            futures = {pool.submit(_run_with_review, t): t for t in batch}
            try:
                for fut in as_completed(futures, timeout=wait_seconds):
                    outcomes.append(fut.result())
            except Exception as e:
                for fut, t in futures.items():
                    if not fut.done():
                        rep = Report(t.key, t.text, "failed", f"timed out after {wait_seconds}s ({e})")
                        outcomes.append(Outcome(rep, Review("reject", ["timed out"])))
    return outcomes


def _context_from(outcomes) -> str:
    parts = [f"[{ROSTER[o.report.specialist].title}] {o.report.text[:700]}" for o in outcomes
             if o.review.verdict == "accept" and o.report.status == "ok"]
    return "\n".join(parts)[:1600]


def _fill_campaign_id(batch, outcomes):
    """ads_operator needs the id of the campaign the strategist just drafted."""
    ids = [m for o in outcomes if o.report.specialist == "ads_strategist" and o.report.status == "ok"
           for m in _ID_RE.findall(o.report.text)]
    for i, t in enumerate(batch):
        if t.key == "ads_operator" and ids and not _ID_RE.search(t.text.split("Results from earlier")[0]):
            batch[i] = Task(t.key, t.text + f"\nThe campaign to push is {ids[0]}.", t.stage)


# --------------------------------------------------------------- conclusion --
_CONFIRM_RE = re.compile(r"'(confirm [a-z ]+?)'", re.IGNORECASE)
_FACT_RE = re.compile(r"\b[cp]_[0-9a-f]{6}\b|https?://\S+|/[\w./~-]+\.\w{2,4}|[$€£₦]\s?\d[\d,.]*")


def _deterministic(outcomes) -> str:
    if len(outcomes) == 1:
        o = outcomes[0]
        return _one(o, header=False)
    return "\n\n".join(_one(o, header=True) for o in outcomes)


def _one(o: Outcome, header: bool) -> str:
    title = ROSTER[o.report.specialist].title
    if o.review.verdict == "accept" and o.report.status == "ok":
        body = o.report.text
    elif o.report.status == "needs_input":
        body = f"I need one thing from you: {o.report.text}"
    else:
        why = "; ".join(o.review.notes) or o.report.text
        body = f"I couldn't finish this, so I'm not claiming it's done. {why[:300]}"
    return f"{title}: {body}" if header else body


def conclude(request: str, outcomes: list, compose=None) -> str:
    """Final answer. With several reports, `compose(system, prompt) -> str|None`
    (the main model + Agatha's voice) may write it, but only if the result is
    grounded: every id/path/URL/amount and every 'confirm ...' phrase it uses
    must come from the verified reports. Otherwise the plain version is used."""
    plain = _deterministic(outcomes)
    if len(outcomes) < 2 or not compose:
        return plain
    try:
        text = compose(
            "You are Agatha, writing the final answer to the user from your specialists' VERIFIED reports. "
            "State only what the reports say. Keep ids, paths, links, amounts and any 'confirm ...' instructions exactly as written. "
            "If a specialist failed or needs something, say so plainly. Be brief.",
            f"User asked: {request}\n\nReports:\n{plain}")
    except Exception:
        return plain
    if not text:
        return plain
    source = plain
    if any(f not in source for f in _FACT_RE.findall(text)):
        return plain
    if any(c.lower() not in text.lower() for c in _CONFIRM_RE.findall(source)):
        return plain
    return text.strip()


def cost_line(outcomes: list) -> str:
    calls = sum(o.report.llm_calls for o in outcomes)
    chars = sum(o.report.prompt_chars for o in outcomes)
    return (f"crew: {len(outcomes)} specialist(s), {calls} LLM call(s), ~{chars // 4} prompt tokens, "
            f"{max((o.report.seconds for o in outcomes), default=0):.0f}s")


def handle(request: str, compose=None):
    """Whole pipeline. Returns (final_text, outcomes) or None if no specialist applies."""
    keys = match_specialists(request)
    if not keys:
        return None
    plan = build_plan(request, keys)
    outcomes = run_plan(plan)
    return conclude(request, outcomes, compose), outcomes
