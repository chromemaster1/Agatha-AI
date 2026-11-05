import os
import json
import uuid
import threading
import urllib.parse
from http.cookies import SimpleCookie
from datetime import datetime
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from tools_research import RESEARCH_DIR
except Exception:
    RESEARCH_DIR = os.path.expanduser("~/Documents/Agent Research")

try:
    from tools_artifacts import list_artifacts, get_artifact_path
except Exception:
    list_artifacts = lambda: []
    get_artifact_path = lambda _id: None

try:
  from tools_settings import SETTING_GROUPS, get_masked_settings, get_agent_providers, set_setting
except Exception:
    SETTING_GROUPS = {}
    get_masked_settings = lambda: {}
    get_agent_providers = lambda: []
    set_setting = lambda k, v: "Settings module unavailable."

try:
    import tools_auth
    import tools_profile
except Exception:
    tools_auth = tools_profile = None

try:
    from tools_subagents import list_subagents_data
except Exception:
    list_subagents_data = lambda: []

try:
  from tools_uploads import MAX_REQUEST_BYTES, parse_multipart_form
except Exception:
  MAX_REQUEST_BYTES = 110 * 1024 * 1024
  parse_multipart_form = None

DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8765"))
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_history.json")
MAX_HISTORY_SESSIONS = 50

_agent_name = "Agent"
_log = deque(maxlen=500)          # current session, live view
_lock = threading.Lock()
_status = {"state": "idle", "detail": ""}
_server_thread = None

# Sandbox: a real trace of the agent's actual processing pipeline (routing,
# tool calls, synthesis) — not a decorative animation. Each set_step() call
# from main_agent.py records a genuine backend event.
_pipeline_trace = deque(maxlen=40)
_current_step = {"stage": "idle", "detail": ""}

# Cross-thread signals consumed by main_agent.py's run loop
new_chat_event = threading.Event()
_inbound_queue = None  # set lazily to a queue.Queue() on first use

_current_session_id = None
authenticated_event = threading.Event()


def set_step(stage: str, detail: str = ""):
    """Called by main_agent.py at each real pipeline stage: routing,
    calling_llm, tool_call, synthesizing, done, idle. Powers the Sandbox
    view's live trace — every entry here is a genuine backend event."""
    entry = {"stage": stage, "detail": detail, "time": datetime.now().strftime("%H:%M:%S")}
    with _lock:
        _current_step["stage"] = stage
        _current_step["detail"] = detail
        _pipeline_trace.append(entry)


def _get_sandbox_json() -> str:
    with _lock:
        return json.dumps({
            "current": dict(_current_step),
            "trace": list(_pipeline_trace),
        })


def _get_inbound_queue():
    global _inbound_queue
    if _inbound_queue is None:
        import queue
        _inbound_queue = queue.Queue()
    return _inbound_queue


def pop_inbound_message(timeout: float = 0.3):
    """Called by main_agent.py's dashboard-input thread. Returns a typed
    message submitted via the dashboard's chat box, or None on timeout."""
    import queue
    try:
        return _get_inbound_queue().get(timeout=timeout)
    except queue.Empty:
        return None


def submit_user_message(message):
  if isinstance(message, dict):
    queued_message = dict(message)
    text = (queued_message.get("text") or "").strip()
    attachments = queued_message.get("attachments", [])
    attachment_names = ", ".join(item.get("name", "file") for item in attachments)
    display_text = f"{text}\n[Attached: {attachment_names}]".strip() if attachment_names else text
  else:
    text = str(message).strip()
    display_text = text
    queued_message = {"text": text}

  if not display_text:
    return
  add_log("You", display_text)
  queued_message["_dashboard_logged"] = True
  _get_inbound_queue().put(queued_message)


def set_agent_name(name: str):
    global _agent_name
    _agent_name = name


def _ensure_session():
    global _current_session_id
    if _current_session_id is None:
        _current_session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _current_session_id


def start_new_session():
    global _current_session_id
    _current_session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    with _lock:
        _log.clear()
    return _current_session_id


def resume_session(session_id: str) -> bool:
  global _current_session_id
  session = _history_session(session_id)
  if session is None:
    return False
  _current_session_id = session_id
  with _lock:
    _log.clear()
    _log.extend(session.get("turns", [])[-_log.maxlen:])
  _get_inbound_queue().put({"action": "resume_session", "session": session})
  return True


def add_log(speaker: str, text: str):
    """speaker: 'You' | 'Agent' | 'System'"""
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "speaker": speaker, "text": text}
    with _lock:
        _log.append(entry)
    _append_to_history(entry)


def set_status(state: str, detail: str = ""):
    with _lock:
        _status["state"] = state
        _status["detail"] = detail


def _get_log_json() -> str:
    with _lock:
        return json.dumps({"log": list(_log), "status": dict(_status), "agent_name": _agent_name})


# ---------------------------------------------------------------------------
# Persistent chat history (survives restarts — this is what powers the
# History tab; the in-memory deque above is only the live current view)
# ---------------------------------------------------------------------------
def _load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _save_history(data: list):
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Warning] Could not save chat history: {e}")


def _append_to_history(entry: dict):
    try:
        sid = _ensure_session()
        data = _load_history()
        session = next((s for s in data if s["id"] == sid), None)
        if session is None:
            session = {"id": sid, "started": datetime.now().isoformat(timespec="seconds"), "turns": []}
            data.append(session)
        session["turns"].append(entry)
        data = data[-MAX_HISTORY_SESSIONS:]
        _save_history(data)
    except Exception as e:
        print(f"[Warning] Could not persist chat history entry: {e}")


def _history_summary() -> list:
    data = _load_history()
    out = []
    for s in data:
        preview = ""
        for t in s.get("turns", []):
            if t["speaker"] == "You":
                preview = t["text"][:80]
                break
        out.append({
            "id": s["id"], "started": s.get("started", ""),
            "turn_count": len(s.get("turns", [])), "preview": preview,
        })
    return list(reversed(out))


def _history_session(session_id: str):
    data = _load_history()
    for s in data:
        if s["id"] == session_id:
            return s
    return None


_HTML_PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Dashboard</title>
<style>
  :root {
      --bg: #121614; --panel: #1a201d; --panel2: #232b26; --border: #38423b;
      --text: #edf2eb; --muted: #9da99f; --accent: #c1e0a7; --accent-dim: #324334;
      --you: #28352c; --agent: #202c29; --system: #382d26;
      --ok: #b6d59c; --warn: #e8ba72; --rose: #d98e82;
  }
  * { box-sizing: border-box; }
    body { margin:0; font-family: "Avenir Next", Avenir, "Trebuchet MS", sans-serif;
      background:radial-gradient(ellipse at 68% -12%, #31443a 0, transparent 43%),
            linear-gradient(155deg, #121614 0, #151b17 48%, #111513 100%);
      color:var(--text); display:flex; height:100vh; overflow:hidden; }

    #sidebar { width:248px; background:linear-gradient(180deg, #1d2721, #171d19 68%, #141916);
          border-right:1px solid var(--border);
             display:flex; flex-direction:column; padding:16px 10px; flex-shrink:0;
             transition: width 0.15s ease, padding 0.15s ease; overflow:hidden; }
  #sidebar.collapsed { width:0; padding-left:0; padding-right:0; border-right:none; }
  #agentName { font-family: "Palatino Linotype", "Book Antiqua", Georgia, serif;
               font-size:20px; font-weight:700; padding:9px 12px 20px; white-space:nowrap; }
  #accountArea { margin-top:auto; padding:12px 10px 0; border-top:1px solid var(--border); }
  #accountName { color:var(--muted); font-size:12px; margin-bottom:9px; overflow:hidden; text-overflow:ellipsis; }
  #logoutBtn { width:100%; text-align:left; }
  .navbtn { display:flex; align-items:center; gap:10px; min-height:44px; padding:10px 12px; border-radius:8px;
            color:var(--text); cursor:pointer; font-size:14px; margin-bottom:4px; user-select:none;
            white-space:nowrap; border:1px solid transparent; transition:background .16s ease, border-color .16s ease; }
  .navbtn:hover { background:#27352c; border-color:#46564a; }
  .navbtn.active { background:linear-gradient(110deg, #344637, #26352b); border-color:#607861;
                   color:var(--accent); font-weight:600; }
  .navbtn .icon { display:grid; place-items:center; width:24px; height:24px; border-radius:7px;
                  background:#2b372f; color:var(--accent); font-size:15px; }

  #main { flex:1; display:flex; flex-direction:column; min-width:0; position:relative; }
  #topbar { padding:14px 24px; border-bottom:1px solid #354039; display:flex;
            align-items:center; justify-content:space-between; flex-shrink:0; gap:12px; }
  #topbarLeft { display:flex; align-items:center; gap:14px; }
  #menuToggle { background:var(--panel2); border:1px solid var(--border); color:var(--text);
                border-radius:8px; width:34px; height:34px; cursor:pointer; font-size:15px; flex-shrink:0; }
  #menuToggle:hover { border-color:var(--accent); color:var(--accent); }
  #viewTitle { font-family:"Palatino Linotype", "Book Antiqua", Georgia, serif; font-size:19px; font-weight:600; }
  #statusPill { padding:5px 12px; border-radius:20px; font-size:12px; font-weight:600; }
  .state-idle { background:#26332b; color:var(--ok); }
  .state-working { background:#3a2f1c; color:var(--warn); }

  /* Sandbox view */
  #sandboxView { padding:24px; overflow-y:auto; background:linear-gradient(180deg, #17201a70, transparent 38%); }
  #pipeline { display:flex; gap:8px; margin-bottom:22px; flex-wrap:wrap; }
  .stageBox { flex:1; min-width:120px; background:linear-gradient(145deg, #27342b, #1d2621);
              border:1px solid #3b4a3e; border-radius:9px; padding:16px 14px; text-align:center; font-size:13px; color:var(--muted); }
  .stageBox.active { border-color:var(--accent); color:var(--accent); background:var(--accent-dim); font-weight:600; }
  .stageBox .stageDetail { font-size:11px; margin-top:4px; color:var(--muted); font-weight:400; }
  #sandboxView h3 { font-size:14px; color:var(--muted); margin:20px 0 10px 0; text-transform:uppercase; letter-spacing:0.04em; }
  .traceRow { font-size:12px; color:var(--muted); padding:5px 0; border-bottom:1px solid var(--border); }
  .traceRow b { color:var(--text); }
  .agentCard { background:linear-gradient(125deg, #29362c, #1d2822); border:1px solid #3c4b3e;
               border-radius:9px; padding:16px 18px; margin-bottom:10px; box-shadow:0 10px 25px #0a110c40; }
  .agentCard .agTitle { font-family:"Palatino Linotype", Georgia, serif; font-weight:600; font-size:17px; }
  .agentCard .agMeta { color:var(--muted); font-size:12px; margin-top:2px; }

  .view { flex:1; overflow:hidden; display:none; flex-direction:column; min-height:0; }
  .view.active { display:flex; }

  /* Chat view */
  #chatLog { flex:1; overflow-y:auto; padding:26px max(24px, calc((100% - 900px) / 2));
             scroll-behavior:smooth; scrollbar-color:#536956 transparent; }
  .entry { max-width:760px; margin-bottom:16px; padding:15px 18px; border:1px solid #3a493e;
           border-radius:9px; line-height:1.55; font-size:14px; box-shadow:0 8px 26px #0b100d30; }
  .entry.You { background:linear-gradient(140deg, #2d3d31, #26332b); margin-left:auto; }
  .entry.Agent { background:linear-gradient(140deg, #26352f, #1d2925); }
  .entry.System { background:var(--system); font-style:italic; color:var(--muted); font-size:13px; }
  .entry .meta { font-size:11px; color:var(--muted); margin-bottom:3px; }
  #chatInputBar { border-top:1px solid var(--border); padding:14px max(20px, calc((100% - 920px) / 2));
                  display:flex; gap:10px; flex-shrink:0; align-items:center; background:#171e19e8; }
  #chatInput { flex:1; background:var(--panel2); border:1px solid var(--border); color:var(--text);
               border-radius:10px; padding:11px 14px; font-size:14px; outline:none; }
  #chatInput:focus { border-color:var(--accent); box-shadow:0 0 0 3px #c1e0a71a; }
  #attachBtn { width:42px; height:42px; padding:0; flex:0 0 42px; font-size:20px; }
  #attachmentList { display:flex; gap:7px; flex-wrap:wrap; padding:0 20px 10px; }
  .attachmentChip { display:flex; align-items:center; gap:7px; max-width:240px; border:1px solid var(--border);
                    background:var(--panel2); border-radius:8px; padding:5px 8px; font-size:12px; }
  .attachmentChip img { width:30px; height:30px; object-fit:cover; border-radius:4px; }
  .attachmentChip span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .attachmentChip button { border:0; background:transparent; color:var(--muted); cursor:pointer; padding:2px 4px; }
  .chatLink { color:var(--accent); text-decoration:underline; overflow-wrap:anywhere; }
  @media (max-width:700px) {
    #chatInputBar { padding:10px 12px; gap:7px; }
    #attachmentList { padding:0 12px 8px; }
    #chatInput { min-width:0; }
    button.send { padding:10px 13px; }
  }
  button.send, button.action { background:var(--accent); color:#0e1420; border:none; border-radius:10px;
              padding:11px 18px; font-weight:600; cursor:pointer; font-size:14px; }
  button.send:hover, button.action:hover { opacity:0.9; }
  button.ghost { background:transparent; border:1px solid var(--border); color:var(--text);
                 border-radius:8px; padding:8px 14px; cursor:pointer; font-size:13px; }
  button.ghost:hover { border-color:var(--accent); color:var(--accent); }

  /* History view */
  #historyView { padding:20px 24px; overflow:hidden; gap:18px; flex-direction:row; }
  #historyList { flex:0 0 min(360px, 38%); overflow-y:auto; min-width:0; }
  .sessionCard { display:block; width:100%; text-align:left;
                 background:linear-gradient(130deg, #27342b, #1d2621); border:1px solid #3c4a3e;
                 border-radius:9px; padding:17px 18px; margin-bottom:10px; cursor:pointer; color:var(--text); }
  .sessionCard:hover { border-color:#819b79; transform:translateY(-1px); }
  .sessionCard.selected { border-color:var(--accent); background:var(--accent-dim); }
  .sessionCard .sTitle { font-weight:600; font-size:14px; }
  .sessionCard .sMeta { color:var(--muted); font-size:12px; margin-top:3px; }
  #historyDetail { flex:1; min-width:0; overflow-y:auto; padding:0 8px 12px 0; }
  .historyDetailHeader { display:flex; justify-content:space-between; align-items:center; gap:12px;
                         padding:4px 0 14px; border-bottom:1px solid var(--border); margin-bottom:14px; }
  @media (max-width:700px) {
    #historyView { flex-direction:column; gap:10px; }
    #historyList { flex:0 0 auto; max-height:34%; }
    #historyDetail { flex:1; min-height:0; }
  }

  /* Artifacts view */
  #artifactsView { padding:20px 24px; overflow-y:auto; }
  .artCard { display:flex; justify-content:space-between; align-items:center; gap:18px;
             background:linear-gradient(125deg, #2a372d, #1c2721); border:1px solid #3d4c40;
             border-radius:9px; padding:18px 20px; margin-bottom:12px; box-shadow:0 10px 24px #0b100d35; }
  .artCard .aTitle { font-family:"Palatino Linotype", Georgia, serif; font-weight:600; font-size:17px; }
  .artCard .aMeta { color:var(--muted); font-size:12px; }
  .artCard a { color:var(--accent); text-decoration:none; font-size:13px; font-weight:600; }

  /* Settings view */
  #settingsView { padding:24px; overflow-y:auto; width:100%; max-width:1060px; margin:0 auto; }
  #settingsNav { display:flex; align-items:center; gap:14px; min-height:46px; margin-bottom:16px; }
  #settingsBack { display:flex; align-items:center; gap:8px; padding:8px 12px; }
  #settingsBack[hidden] { display:none; }
  #settingsCrumb { color:var(--muted); font-size:13px; }
  #settingsList { display:grid; grid-template-columns:repeat(auto-fit, minmax(min(100%, 360px), 1fr)); gap:14px; }
  .agentSearch { max-width:540px; margin:0 0 16px; }
  .settingsScreen { display:none; }
  .settingsScreen.active { display:block; }
  .settingsScreen .settingsGrid { display:grid; grid-template-columns:repeat(auto-fit, minmax(min(100%, 360px), 1fr)); gap:14px; }
  .settingsScreenTitle { margin:0 0 16px; font-family:"Palatino Linotype", Georgia, serif; font-size:23px; }
  .agentConnectionNote { color:var(--muted); font-size:13px; line-height:1.45; margin:4px 0 16px; }
  .agentCouncilNotice { padding:12px 14px; margin:0 0 16px; border-left:3px solid #91ad86;
                        background:#202c24; color:#c6d0c4; font-size:12px; line-height:1.5; }
  .settingsGroup { min-width:0; align-self:start; background:linear-gradient(145deg, #292b31, #222329 72%);
                   border:1px solid #41434a; border-radius:10px; overflow:hidden; }
  .settingsHero { display:flex; align-items:center; gap:14px; width:100%; min-height:100px; padding:18px;
                  border:0; text-align:left; color:var(--text); background:transparent; cursor:pointer; }
  .settingsHero:hover { background:#303239; }
  .settingsIcon { display:grid; place-items:center; flex:0 0 46px; height:46px; border-radius:9px;
                  background:#39433c; color:#b5d4bd; font-size:20px; font-weight:700; }
  .settingsHeading { flex:1; min-width:0; }
  .settingsHeading strong { display:block; font-size:16px; }
  .settingsHeading span { display:block; margin-top:5px; color:var(--muted); font-size:12px; }
  .settingsChevron { color:var(--muted); font-size:18px; transition:transform .18s ease; }
  .settingsHero[aria-expanded="true"] .settingsChevron { transform:rotate(90deg); }
  .settingsChildren { padding:3px 18px 16px; border-top:1px solid var(--border); }
  .settingsChildren[hidden] { display:none; }
  .settingsGroup { background:linear-gradient(145deg, #293a2d, #1d2821 74%); border-color:#465a49;
                   box-shadow:0 12px 30px #0a110c45; }
  .settingRow { display:flex; align-items:center; gap:10px; margin-bottom:8px; }
  .settingRow:first-child { margin-top:14px; }
  .settingRow label { width:190px; font-size:12px; flex-shrink:0; overflow-wrap:anywhere; }
  .settingRow input { flex:1; background:var(--panel2); border:1px solid var(--border); color:var(--text);
                       border-radius:8px; padding:8px 10px; font-size:13px; }
  .settingRow input:focus { outline:2px solid #c1e0a755; border-color:#91ad86; }
  .settingActions { display:flex; align-items:center; gap:8px; min-width:116px; justify-content:flex-end; }
  .settingRow .setBadge { font-size:11px; color:var(--ok); flex-shrink:0; }
  .settingSave { border:0; border-radius:6px; padding:8px 12px; color:#172018; background:#c1e0a7;
                 font-size:12px; font-weight:700; cursor:pointer; }
  .settingSave:hover { background:#d5edbf; }
  .settingFeedback { min-height:16px; margin:4px 0 0; color:var(--muted); font-size:11px; }
  .settingFeedback.success { color:var(--ok); }
  .settingFeedback.error { color:var(--rose); }
  .empty { color:var(--muted); font-style:italic; padding:10px 0; }
  #authGate { position:fixed; z-index:1000; inset:0; display:flex; align-items:flex-start; justify-content:center; padding:22px;
              overflow-y:auto;
              background:radial-gradient(ellipse at 75% 15%, #3b463d 0, transparent 38%),
                         linear-gradient(135deg, #171a19, #202320 55%, #252923); }
  #authGate[hidden] { display:none; }
  .authPanel { width:min(100%, 440px); margin:auto 0; padding:32px; border:1px solid #525950; border-radius:10px;
               background:linear-gradient(145deg, #293a2d, #1c241f); box-shadow:0 24px 70px #080a0870; }
  .authMark { color:#c1e0a7; font-size:12px; font-weight:700; text-transform:uppercase; }
  .authPanel h1 { margin:14px 0 8px; font-family:"Palatino Linotype", Georgia, serif; font-size:30px; }
  .authPanel p { margin:0 0 24px; color:var(--muted); line-height:1.5; font-size:14px; }
  .authPanel label { display:block; margin:14px 0 6px; font-size:12px; color:#c7ccc6; }
  .authPanel input { width:100%; padding:11px 12px; border:1px solid #484d47; border-radius:7px;
                     background:#171a18; color:var(--text); font-size:14px; }
  .rememberControl { display:flex !important; align-items:center; gap:9px; margin:12px 0 0 !important;
                     color:#c7ccc6; cursor:pointer; }
  .rememberControl input { flex:0 0 16px; width:16px; height:16px; margin:0; padding:0;
                           accent-color:#c1e0a7; }
  .passwordControl { display:flex; gap:8px; align-items:center; }
  .passwordControl input { flex:1; min-width:0; }
  .passwordToggle { flex:0 0 auto; border:1px solid #526151 !important; color:var(--text) !important;
                    background:#28352b !important; padding:9px 11px !important; font-size:12px !important; }
  .authPanel .passwordToggle { width:auto; margin:0; }
  .authModeToggle { width:100%; margin-top:12px !important; border:1px solid #526151 !important;
                    color:var(--text) !important; background:transparent !important; }
  .accountField { width:100%; margin:0 0 12px; padding:10px 11px; border:1px solid var(--border);
                  border-radius:7px; color:var(--text); background:#171d19; font-size:13px; }
  .accountField:focus { outline:2px solid #c1e0a755; border-color:#91ad86; }
  .authPanel input:focus { outline:2px solid #c1e0a755; border-color:#91ad86; }
  .authPanel button { width:100%; margin-top:20px; padding:12px; border:0; border-radius:7px;
                      color:#182019; background:#c1e0a7; font-size:14px; font-weight:700; cursor:pointer; }
  #authMessage { min-height:18px; margin-top:12px; color:#f0a99b; font-size:13px; }
  @media (max-width:700px) {
    #sidebar { width:210px; }
    #settingsView { padding:14px; }
    .settingRow { flex-wrap:wrap; }
    .settingRow label { width:100%; }
    .settingRow input { min-width:80px; }
    .settingActions { width:100%; }
    .authPanel { padding:25px; }
    #chatLog { padding:16px 12px; }
    #chatInputBar { padding:10px 12px; }
    #historyView { padding:14px; }
  }
  @media (max-width:460px) {
    #sidebar { width:190px; padding:12px 7px; }
    #topbar { padding:11px 12px; }
    #settingsList { grid-template-columns:minmax(0, 1fr); }
    .entry { max-width:94%; }
  }
</style>
</head>
<body>

<div id="authGate" hidden>
  <form class="authPanel" id="authForm">
    <div class="authMark">Agatha · Local account</div>
    <h1 id="authTitle">Welcome back</h1>
    <p id="authDescription">Sign in to continue to your private, local Agatha workspace.</p>
    <label for="authIdentifier">Username or email</label>
    <input id="authIdentifier" name="identifier" autocomplete="username" maxlength="254" required>
    <div id="signupNameField" hidden>
      <label for="authName">What should Agatha call you?</label>
      <input id="authName" name="name" autocomplete="name" maxlength="80">
    </div>
    <label for="authPassword">Password</label>
    <div class="passwordControl"><input id="authPassword" name="password" type="password" autocomplete="current-password" required>
      <button class="passwordToggle" type="button" data-toggle-password="authPassword" aria-pressed="false">Show</button></div>
    <label class="rememberControl"><input id="rememberSession" type="checkbox" checked> Keep me signed in on this device</label>
    <button id="authSubmit" type="submit">Sign in</button>
    <button id="authModeToggle" class="authModeToggle" type="button">Create an account</button>
    <div id="authMessage" role="status" aria-live="polite"></div>
  </form>
</div>

<div id="sidebar">
  <div id="agentName">Agent</div>
  <div class="navbtn active" data-view="chat" data-action="current-chat" aria-current="page"><span class="icon">◷</span> Current conversation</div>
  <div class="navbtn" data-view="chat" data-action="new-chat"><span class="icon">＋</span> New Chat</div>
  <div class="navbtn" data-view="history"><span class="icon">🕘</span> Chat History</div>
  <div class="navbtn" data-view="sandbox"><span class="icon">🧪</span> Sandbox</div>
  <div class="navbtn" data-view="artifacts"><span class="icon">📄</span> Artifacts</div>
  <div class="navbtn" data-view="settings"><span class="icon">⚙️</span> Settings</div>
  <div id="accountArea"><div id="accountName">Local account</div><button id="logoutBtn" class="ghost" type="button">Sign out</button></div>
</div>

<div id="main">
  <div id="topbar">
    <div id="topbarLeft">
      <button id="menuToggle" title="Toggle menu">☰</button>
      <div id="viewTitle">Chat</div>
    </div>
    <div id="statusPill" class="state-idle">Idle</div>
  </div>

  <div id="chatView" class="view active">
    <div id="chatLog"></div>
    <div id="attachmentList" aria-live="polite"></div>
    <div id="chatInputBar">
      <input id="fileInput" type="file" multiple accept=".txt,.md,.csv,.json,.py,.html,.xml,.yaml,.yml,.log,.rst,.tex,.sql,.ini,.toml,.cfg,.conf,.sh,.bash,.zsh,.ps1,.bat,.c,.h,.cpp,.java,.go,.rs,.rb,.php,.css,.js,.ts,.jsx,.tsx,.vue,.srt,.vtt,.ics,.eml,.pdf,.docx,.odt,.pptx,.xlsx,.rtf,.epub,.url,.webloc,.png,.jpg,.jpeg,.webp,.gif,.mp4,.m4v,.mov,.webm,.avi,.mkv,.mp3,audio/mpeg" hidden />
      <button class="ghost" id="attachBtn" type="button" title="Attach files, images, audio, or videos" aria-label="Attach files, images, audio, or videos">＋</button>
      <input id="chatInput" type="text" placeholder="Type a message… (also spoken aloud by the agent)" />
      <button class="send" id="sendBtn">Send</button>
    </div>
  </div>

  <div id="historyView" class="view">
    <div id="historyList"></div>
    <div id="historyDetail"></div>
  </div>

  <div id="sandboxView" class="view">
    <h3>Live Pipeline</h3>
    <div id="pipeline"></div>
    <h3>Recent Trace</h3>
    <div id="traceList"></div>
    <h3>Custom Agents</h3>
    <div id="agentsList"></div>
  </div>

  <div id="artifactsView" class="view">
    <div id="artifactsList"></div>
  </div>

  <div id="settingsView" class="view">
    <div id="settingsNav"><button class="ghost" id="settingsBack" type="button" hidden>← Back</button>
      <div id="settingsCrumb">Settings</div></div>
    <div id="settingsList"></div>
  </div>
</div>

<script>
function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escAttr(s) { return esc(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function chatText(s) {
  return esc(s).replace(/\/artifact\/[a-f0-9]{12}\b/g, path =>
    '<a class="chatLink" href="' + path + '">Download file</a>'
  ).replace(/https?:\/\/[^\s<]+/g, url => {
    const clean = url.replace(/[),.!?;]+$/, '');
    return '<a class="chatLink" href="' + clean + '" target="_blank" rel="noopener noreferrer">' + clean + '</a>' + url.slice(clean.length);
  }).replace(/\n/g,'<br>');
}
let currentView = 'chat';
let selectedHistoryId = '';
let selectedFiles = [];
let dashboardAuthenticated = false;
let settingsInitialized = false;
let settingsStack = ['root'];

const authForm = document.getElementById('authForm');
let accountConfigured = true;
let authMode = 'login';

function showAuth(configured) {
  accountConfigured = configured;
  authMode = configured ? 'login' : 'register';
  renderAuthMode();
  document.getElementById('authGate').hidden = false;
}

function renderAuthMode() {
  document.getElementById('authGate').hidden = false;
  const registering = authMode === 'register';
  document.getElementById('authTitle').textContent = registering ? 'Create your account' : 'Welcome back';
  document.getElementById('authDescription').textContent = registering
    ? (accountConfigured ? 'Create a local profile for this device.'
      : 'Create your local profile and choose the name Agatha should use for you.')
    : (accountConfigured ? 'Use your username or email and password to continue to your local workspace.'
      : 'There is no local profile yet. Choose Register to create one.');
  document.getElementById('signupNameField').hidden = !registering;
  document.getElementById('authName').required = registering;
  document.getElementById('authPassword').autocomplete = registering ? 'new-password' : 'current-password';
  document.getElementById('authPassword').minLength = registering ? 10 : 1;
  document.getElementById('authSubmit').textContent = registering ? 'Register' : 'Sign in';
  document.getElementById('authModeToggle').hidden = false;
  document.getElementById('authModeToggle').textContent = registering ? 'I already have an account' : 'Create an account';
}

function setAccountName(name) {
  document.getElementById('accountName').textContent = name ? 'Signed in as ' + name : 'Local account';
}

authForm.addEventListener('submit', async event => {
  event.preventDefault();
  const message = document.getElementById('authMessage');
  message.textContent = '';
  const registering = authMode === 'register';
  const endpoint = registering ? '/api/auth/signup' : '/api/auth/login';
  const payload = {
    identifier: document.getElementById('authIdentifier').value,
    password: document.getElementById('authPassword').value,
    remember: document.getElementById('rememberSession').checked,
  };
  if (registering) payload.name = document.getElementById('authName').value;
  try {
    const response = await fetch(endpoint, {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok) { message.textContent = result.error || 'Could not sign in.'; return; }
    dashboardAuthenticated = true;
    setAccountName(result.account.name);
    document.getElementById('authGate').hidden = true;
    document.getElementById('authIdentifier').value = '';
    document.getElementById('authPassword').value = '';
    await refreshChat();
  } catch (error) { message.textContent = 'Agatha could not be reached. Check that the local app is running.'; }
});

document.getElementById('authModeToggle').addEventListener('click', () => {
  authMode = authMode === 'login' ? 'register' : 'login';
  document.getElementById('authMessage').textContent = '';
  document.getElementById('authName').value = '';
  document.getElementById('authPassword').value = '';
  renderAuthMode();
});

document.querySelectorAll('[data-toggle-password]').forEach(button => button.addEventListener('click', () => {
  const input = document.getElementById(button.dataset.togglePassword);
  const visible = input.type === 'password';
  input.type = visible ? 'text' : 'password';
  button.textContent = visible ? 'Hide' : 'Show';
  button.setAttribute('aria-pressed', visible ? 'true' : 'false');
}));

async function initializeDashboard() {
  try {
    const response = await fetch('/api/session');
    const session = await response.json();
    if (session.authenticated) {
      dashboardAuthenticated = true;
      setAccountName(session.name);
      document.getElementById('authGate').hidden = true;
      await refreshChat();
    } else {
      showAuth(session.configured);
    }
  } catch (error) {
    showAuth(true);
    document.getElementById('authMessage').textContent = 'Agatha could not be reached. Check that the local app is running.';
  }
}

document.getElementById('logoutBtn').addEventListener('click', async () => {
  await fetch('/api/auth/logout', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  dashboardAuthenticated = false;
  showAuth(true);
});

const MENU_KEY = 'agent_dashboard_menu_collapsed';
const sidebarEl = document.getElementById('sidebar');
if (localStorage.getItem(MENU_KEY) === '1') sidebarEl.classList.add('collapsed');
document.getElementById('menuToggle').addEventListener('click', () => {
  sidebarEl.classList.toggle('collapsed');
  localStorage.setItem(MENU_KEY, sidebarEl.classList.contains('collapsed') ? '1' : '0');
});

document.querySelectorAll('.navbtn').forEach(btn => {
  btn.addEventListener('click', () => {
    if (btn.dataset.action === 'new-chat') startNewChat();
    switchView(btn.dataset.view);
  });
});

function switchView(view) {
  currentView = view;
  document.querySelectorAll('.navbtn').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById(view + 'View').classList.add('active');
  const titles = {chat:'Chat', history:'Chat History', sandbox:'Sandbox', artifacts:'Artifacts', settings:'Settings'};
  document.getElementById('viewTitle').textContent = titles[view];
  if (view === 'history') loadHistory();
  if (view === 'sandbox') loadSandbox();
  if (view === 'artifacts') loadArtifacts();
  if (view === 'settings') loadSettings();
}

async function startNewChat() {
  document.getElementById('chatLog').innerHTML = '';
  try { await fetch('/api/new_chat', {method:'POST'}); } catch(e) {}
}

async function refreshChat() {
  if (!dashboardAuthenticated) return;
  try {
    const res = await fetch('/api/log');
    if (res.status === 401) {
      dashboardAuthenticated = false;
      showAuth(true);
      return;
    }
    const data = await res.json();
    document.getElementById('agentName').textContent = data.agent_name || 'Agent';
    const statusEl = document.getElementById('statusPill');
    if (data.status.state === 'working') {
      statusEl.textContent = 'Working: ' + data.status.detail;
      statusEl.className = 'state-working';
    } else {
      statusEl.textContent = 'Idle';
      statusEl.className = 'state-idle';
    }
    const logEl = document.getElementById('chatLog');
    const shouldFollow = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 80;
    const previousScrollTop = logEl.scrollTop;
    if (data.log.length === 0) {
      logEl.innerHTML = '<div class="empty">No activity yet — say or type something to get started.</div>';
    } else {
      logEl.innerHTML = data.log.map(e =>
        '<div class="entry ' + e.speaker + '"><div class="meta">' + e.speaker + ' · ' + e.time + '</div>' +
        chatText(e.text) + '</div>'
      ).join('');
    }
    logEl.scrollTop = shouldFollow ? logEl.scrollHeight : previousScrollTop;
  } catch (e) { /* ignore transient errors */ }
}

document.getElementById('sendBtn').addEventListener('click', sendChat);
document.getElementById('chatInput').addEventListener('keydown', e => { if (e.key === 'Enter') sendChat(); });
document.getElementById('attachBtn').addEventListener('click', () => document.getElementById('fileInput').click());
document.getElementById('fileInput').addEventListener('change', e => {
  selectedFiles.push(...Array.from(e.target.files));
  e.target.value = '';
  renderAttachments();
});

function renderAttachments() {
  const list = document.getElementById('attachmentList');
  list.innerHTML = selectedFiles.map((file, index) => {
    const preview = file.type.startsWith('image/') ? '<img src="' + URL.createObjectURL(file) + '" alt="">' : '';
    return '<div class="attachmentChip">' + preview + '<span title="' + esc(file.name) + '">' + esc(file.name) + '</span>' +
      '<button type="button" data-remove-file="' + index + '" title="Remove attachment" aria-label="Remove ' + esc(file.name) + '">×</button></div>';
  }).join('');
  list.querySelectorAll('[data-remove-file]').forEach(button => button.addEventListener('click', () => {
    selectedFiles.splice(Number(button.dataset.removeFile), 1);
    renderAttachments();
  }));
}

async function sendChat() {
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  if (!text && !selectedFiles.length) return;
  const form = new FormData();
  form.append('text', text);
  selectedFiles.forEach(file => form.append('files', file, file.name));
  const response = await fetch('/api/send', {method:'POST', body: form});
  const result = await response.json();
  if (!response.ok) {
    alert(result.error || 'Could not send this message.');
    return;
  }
  input.value = '';
  selectedFiles = [];
  renderAttachments();
}

async function loadHistory() {
  const res = await fetch('/api/history');
  const sessions = await res.json();
  const listEl = document.getElementById('historyList');
  listEl.innerHTML = sessions.length ? sessions.map(s =>
    '<button class="sessionCard' + (selectedHistoryId === s.id ? ' selected' : '') + '" onclick="loadSession(\'' + s.id + '\')" aria-pressed="' + (selectedHistoryId === s.id) + '">' +
    '<div class="sTitle">' + esc(s.preview || '(no user message)') + '</div>' +
    '<div class="sMeta">' + esc(s.started) + ' · ' + s.turn_count + ' turns</div></button>'
  ).join('') : '<div class="empty">No past sessions yet.</div>';
}

async function loadSession(id) {
  const res = await fetch('/api/history/' + encodeURIComponent(id));
  const session = await res.json();
  selectedHistoryId = id;
  document.querySelectorAll('.sessionCard').forEach(card => {
    const selected = card.getAttribute('onclick') === "loadSession('" + id + "')";
    card.classList.toggle('selected', selected);
    card.setAttribute('aria-pressed', selected ? 'true' : 'false');
  });
  const detailEl = document.getElementById('historyDetail');
  if (!session || !session.turns) { detailEl.innerHTML = '<div class="empty">Session not found.</div>'; return; }
  detailEl.innerHTML = '<div class="historyDetailHeader"><div><strong>Conversation</strong>' +
    '<div class="sMeta">Started ' + esc(session.started || '') + '</div></div>' +
    '<button class="action" id="continueSessionBtn">Continue conversation</button></div>' +
    session.turns.map(e =>
    '<div class="entry ' + e.speaker + '"><div class="meta">' + e.speaker + ' · ' + e.time + '</div>' +
    esc(e.text).replace(/\n/g,'<br>') + '</div>'
  ).join('');
  document.getElementById('continueSessionBtn').addEventListener('click', () => continueSession(id));
}

async function continueSession(id) {
  const res = await fetch('/api/resume_chat', {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id})
  });
  if (!res.ok) {
    document.getElementById('historyDetail').insertAdjacentHTML('afterbegin',
      '<div class="empty">Could not resume this conversation.</div>');
    return;
  }
  switchView('chat');
  await refreshChat();
  document.getElementById('viewTitle').textContent = 'Continuing conversation';
  const chatInput = document.getElementById('chatInput');
  chatInput.placeholder = 'Continue this conversation…';
  chatInput.focus();
}

async function loadArtifacts() {
  const res = await fetch('/api/artifacts');
  const items = await res.json();
  const el = document.getElementById('artifactsList');
  el.innerHTML = items.length ? items.map(a =>
    '<div class="artCard"><div><div class="aTitle">' + esc(a.title) + '</div>' +
    '<div class="aMeta">' + a.kind + ' · ' + a.created + '</div></div>' +
    '<a href="/artifact/' + a.id + '" target="_blank">Download</a></div>'
  ).join('') : '<div class="empty">No downloadable files generated yet.</div>';
}

const PIPELINE_STAGES = [
  {key:'routing', label:'Routing'},
  {key:'calling_llm', label:'Calling LLM'},
  {key:'tool_call', label:'Tool Call'},
  {key:'synthesizing', label:'Synthesizing'},
  {key:'done', label:'Done'},
];

async function loadSandbox() {
  await refreshSandbox();
  const res = await fetch('/api/subagents');
  const agents = await res.json();
  const el = document.getElementById('agentsList');
  el.innerHTML = agents.length ? agents.map(a =>
    '<div class="agentCard"><div class="agTitle">' + esc(a.name) + '</div>' +
    '<div class="agMeta">' + esc(a.purpose) + ' · ' + a.turns + ' past turns</div></div>'
  ).join('') : '<div class="empty">No custom agents yet — say "create an agent named X to Y".</div>';
}

async function refreshSandbox() {
  try {
    const res = await fetch('/api/sandbox');
    const data = await res.json();
    const pipelineEl = document.getElementById('pipeline');
    pipelineEl.innerHTML = PIPELINE_STAGES.map(s => {
      const isActive = data.current.stage === s.key;
      const detail = isActive && data.current.detail ? '<div class="stageDetail">' + esc(data.current.detail) + '</div>' : '';
      return '<div class="stageBox' + (isActive ? ' active' : '') + '">' + s.label + detail + '</div>';
    }).join('');

    const traceEl = document.getElementById('traceList');
    const trace = data.trace.slice().reverse();
    traceEl.innerHTML = trace.length ? trace.map(t =>
      '<div class="traceRow"><b>' + t.time + '</b> — ' + esc(t.stage) + (t.detail ? ': ' + esc(t.detail) : '') + '</div>'
    ).join('') : '<div class="empty">No activity yet.</div>';
  } catch (e) { /* ignore transient errors */ }
}

async function loadSettings() {
  if (settingsInitialized) {
    showSettingsScreen(settingsStack[settingsStack.length - 1], false);
    return;
  }
  const res = await fetch('/api/settings');
  const data = await res.json();
  const sessionResponse = await fetch('/api/session');
  const account = await sessionResponse.json();
  const el = document.getElementById('settingsList');
  const menuCard = (id, icon, title, description) => `<button class="settingsHero" type="button" data-open-screen="${id}">
    <span class="settingsIcon" aria-hidden="true">${icon}</span>
    <span class="settingsHeading"><strong>${esc(title)}</strong><span>${esc(description)}</span></span>
    <span class="settingsChevron" aria-hidden="true">›</span></button>`;
  const settingForm = (key) => `<div class="settingRow"><label for="set_${escAttr(key)}">${esc(key)}</label>
    <input type="password" id="set_${escAttr(key)}" placeholder="${data.values[key].set ? escAttr(data.values[key].masked) : 'not set'}" />
    <div class="settingActions"><span class="setBadge">${data.values[key].set ? '● set' : ''}</span>
    <button class="settingSave" type="button" data-save-setting="${escAttr(key)}">Save</button></div></div>
    <div class="settingFeedback" id="feedback_${escAttr(key)}" role="status" aria-live="polite"></div>`;
  const platforms = Object.entries(data.groups);
  const agents = data.agents || [];
  const platformScreens = platforms.map(([platform, keys]) => `<section class="settingsScreen" data-screen="social:${escAttr(platform)}">
    <h2 class="settingsScreenTitle">${esc(platform)}</h2><div class="settingsGroup"><div class="settingsChildren">
    ${keys.map(settingForm).join('')}</div></div></section>`).join('');
  el.innerHTML = `<section class="settingsScreen active" data-screen="root"><div class="settingsGrid">
    ${menuCard('profile', 'P', 'Profile', 'Update your name and sign-in username or email')}
    ${menuCard('password', '*', 'Password', 'Change your local Agatha account password')}
    ${menuCard('socials', '↗', 'Link socials', 'Connect social platforms and manage credentials')}
    ${menuCard('agents', 'AI', 'Connect agents', 'Link AI providers for independent review and council answers')}
    </div></section>
    <section class="settingsScreen" data-screen="profile"><h2 class="settingsScreenTitle">Profile</h2>
      <div class="settingsGroup"><div class="settingsChildren">
        <label for="profileName">Preferred name</label>
        <input class="accountField" id="profileName" maxlength="80" value="${escAttr(account.name || '')}">
        <label for="profileIdentifier">Username or email</label>
        <input class="accountField" id="profileIdentifier" maxlength="254" value="${escAttr(account.identifier || '')}">
        <button class="settingSave" type="button" id="saveProfile">Save profile</button>
        <div class="settingFeedback" id="profileFeedback" role="status" aria-live="polite"></div>
      </div></div></section>
    <section class="settingsScreen" data-screen="password"><h2 class="settingsScreenTitle">Password</h2>
      <div class="settingsGroup"><div class="settingsChildren">
        <label for="currentPassword">Current password</label>
        <div class="passwordControl"><input class="accountField" id="currentPassword" type="password" autocomplete="current-password">
          <button class="passwordToggle" type="button" data-toggle-password="currentPassword" aria-pressed="false">Show</button></div>
        <label for="newPassword">New password</label>
        <div class="passwordControl"><input class="accountField" id="newPassword" type="password" minlength="10" autocomplete="new-password">
          <button class="passwordToggle" type="button" data-toggle-password="newPassword" aria-pressed="false">Show</button></div>
        <label for="confirmPassword">Confirm new password</label>
        <div class="passwordControl"><input class="accountField" id="confirmPassword" type="password" minlength="10" autocomplete="new-password">
          <button class="passwordToggle" type="button" data-toggle-password="confirmPassword" aria-pressed="false">Show</button></div>
        <button class="settingSave" type="button" id="savePassword">Update password</button>
        <div class="settingFeedback" id="passwordFeedback" role="status" aria-live="polite"></div>
      </div></div></section>
    <section class="settingsScreen" data-screen="socials"><h2 class="settingsScreenTitle">Link socials</h2>
      <div class="settingsGrid">${platforms.map(([platform, keys]) => menuCard(`social:${platform}`, platform.slice(0, 1), platform,
        `${keys.length} credential${keys.length === 1 ? '' : 's'} · configure connection`)).join('')}</div></section>
    <section class="settingsScreen" data-screen="agents"><h2 class="settingsScreenTitle">Connect agents</h2>
      <p class="agentCouncilNotice">When two or more providers are connected, Agatha may share the relevant request, draft, and tool results with them for review. Provider charges and data policies apply. Actions are executed once by Agatha, not repeated by reviewers.</p>
      <input class="accountField agentSearch" id="agentSearch" type="search" placeholder="Search providers or model names">
      <div class="settingsGrid" id="agentProviderList">${agents.map(provider => menuCard(`agent:${provider.id}`, 'AI', provider.name,
        provider.connected ? `Connected · ${provider.model}` : `Not connected · ${provider.model}`)).join('')}</div>
    </section>
    ${agents.map(provider => `<section class="settingsScreen" data-screen="agent:${escAttr(provider.id)}">
      <h2 class="settingsScreenTitle">${esc(provider.name)}</h2><div class="settingsGroup"><div class="settingsChildren">
        <p class="agentConnectionNote">Use a provider API key. Do not enter your provider account password.</p>
        <label for="set_${escAttr(provider.key)}">API key</label>
        <input class="accountField" type="password" id="set_${escAttr(provider.key)}" placeholder="${provider.connected ? escAttr(provider.masked) : 'not connected'}">
        <div class="settingFeedback" id="feedback_${escAttr(provider.key)}" role="status" aria-live="polite"></div>
        <button class="settingSave" type="button" data-save-setting="${escAttr(provider.key)}">Save API key</button>
        <label for="set_${escAttr(provider.model_key)}">Model</label>
        <input class="accountField" type="text" id="set_${escAttr(provider.model_key)}" value="${escAttr(provider.model)}">
        <div class="settingFeedback" id="feedback_${escAttr(provider.model_key)}" role="status" aria-live="polite"></div>
        <button class="settingSave" type="button" data-save-setting="${escAttr(provider.model_key)}">Save model</button>
      </div></div></section>`).join('')}
    ${platformScreens}`;

  el.querySelectorAll('[data-open-screen]').forEach(button => button.addEventListener('click', () =>
    navigateSettings(button.dataset.openScreen)));
  el.querySelectorAll('[data-save-setting]').forEach(button => button.addEventListener('click', () =>
    saveSetting(button.dataset.saveSetting)));
  el.querySelectorAll('[data-toggle-password]').forEach(button => button.addEventListener('click', () => {
    const input = document.getElementById(button.dataset.togglePassword);
    const visible = input.type === 'password';
    input.type = visible ? 'text' : 'password';
    button.textContent = visible ? 'Hide' : 'Show';
    button.setAttribute('aria-pressed', visible ? 'true' : 'false');
  }));
  document.getElementById('saveProfile').addEventListener('click', saveProfile);
  document.getElementById('savePassword').addEventListener('click', savePassword);
  document.getElementById('settingsBack').addEventListener('click', backSettings);
  const agentSearch = document.getElementById('agentSearch');
  agentSearch.addEventListener('input', () => {
    const query = agentSearch.value.trim().toLowerCase();
    document.querySelectorAll('#agentProviderList [data-open-screen]').forEach(card => {
      card.hidden = !card.textContent.toLowerCase().includes(query);
    });
  });
  settingsInitialized = true;
  settingsStack = ['root'];
  showSettingsScreen('root', false);
}

function navigateSettings(screen) {
  settingsStack.push(screen);
  showSettingsScreen(screen, false);
}

function backSettings() {
  if (settingsStack.length <= 1) return;
  settingsStack.pop();
  showSettingsScreen(settingsStack[settingsStack.length - 1], false);
}

function showSettingsScreen(screen) {
  document.querySelectorAll('.settingsScreen').forEach(panel => {
    panel.classList.toggle('active', panel.dataset.screen === screen);
  });
  const backButton = document.getElementById('settingsBack');
  backButton.hidden = settingsStack.length <= 1;
  const labels = {root:'Settings', profile:'Profile', password:'Password', socials:'Link socials', agents:'Connect agents'};
  const crumbs = settingsStack.map(item => labels[item] || item.replace('social:', '').replace('agent:', ''));
  document.getElementById('settingsCrumb').textContent = crumbs.join(' / ');
  document.getElementById('settingsView').scrollTop = 0;
}

async function saveProfile() {
  const feedback = document.getElementById('profileFeedback');
  feedback.textContent = '';
  try {
    const response = await fetch('/api/account/profile', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:document.getElementById('profileName').value,
        identifier:document.getElementById('profileIdentifier').value})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not update your profile.');
    setAccountName(result.account.name);
    feedback.textContent = 'Profile updated.';
    feedback.className = 'settingFeedback success';
  } catch (error) {
    feedback.textContent = error.message || 'Could not update your profile.';
    feedback.className = 'settingFeedback error';
  }
}

async function savePassword() {
  const feedback = document.getElementById('passwordFeedback');
  const currentPassword = document.getElementById('currentPassword');
  const newPassword = document.getElementById('newPassword');
  const confirmPassword = document.getElementById('confirmPassword');
  feedback.textContent = '';
  if (newPassword.value !== confirmPassword.value) {
    feedback.textContent = 'The new passwords do not match.';
    feedback.className = 'settingFeedback error';
    confirmPassword.focus();
    return;
  }
  try {
    const response = await fetch('/api/account/password', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({current_password:currentPassword.value, new_password:newPassword.value})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not update your password.');
    currentPassword.value = '';
    newPassword.value = '';
    confirmPassword.value = '';
    feedback.textContent = result.message || 'Password updated.';
    feedback.className = 'settingFeedback success';
  } catch (error) {
    feedback.textContent = error.message || 'Could not update your password.';
    feedback.className = 'settingFeedback error';
  }
}

async function saveSetting(key) {
  const input = document.getElementById('set_' + key);
  const button = document.querySelector('[data-save-setting="' + CSS.escape(key) + '"]');
  const feedback = document.getElementById('feedback_' + key);
  const value = input.value;
  if (!value.trim()) {
    feedback.textContent = 'Enter a value before saving.';
    feedback.className = 'settingFeedback error';
    input.focus();
    return;
  }
  button.disabled = true;
  button.textContent = 'Saving';
  try {
    const response = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key, value})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not save this setting.');
    const settingRow = input.closest('.settingRow');
    if (!key.startsWith('AGATHA_')) input.value = '';
    if (result.masked) input.placeholder = result.masked;
    const badge = settingRow && settingRow.querySelector('.setBadge');
    if (badge) badge.textContent = '● set';
    feedback.textContent = result.message || 'Saved';
    feedback.className = 'settingFeedback success';
  } catch (error) {
    feedback.textContent = error.message || 'Could not save this setting.';
    feedback.className = 'settingFeedback error';
  } finally {
    button.disabled = false;
    button.textContent = 'Save';
  }
}

setInterval(() => {
  if (!dashboardAuthenticated) return;
  if (currentView === 'chat') refreshChat();
  if (currentView === 'sandbox') refreshSandbox();
}, 1500);
initializeDashboard();
</script>
</body>
</html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silence default per-request console spam

    def _session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get("agatha_session")
        return morsel.value if morsel else ""

    def _is_authenticated(self):
      authenticated = bool(tools_auth and tools_auth.is_authenticated(self._session_token()))
      if authenticated:
        authenticated_event.set()
      else:
        authenticated_event.clear()
      return authenticated

    def _origin_is_local(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urllib.parse.urlsplit(origin).netloc == self.headers.get("Host", "")

    def _send_json(self, code, payload, extra_headers=None):
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json", extra_headers)

    def _send(self, code, body: bytes, content_type: str, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _read_request_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_REQUEST_BYTES:
            raise ValueError("The upload exceeds the 110 MB total request limit.")
        return self.rfile.read(length)

    # --- GET ---
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, _HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/session":
            authenticated = self._is_authenticated()
            if authenticated:
              authenticated_event.set()
            self._send_json(200, {
                "configured": bool(tools_auth and tools_auth.account_exists()),
              "authenticated": authenticated,
                "name": tools_auth.account_name() if tools_auth else "",
                "identifier": tools_auth.account_identifier() if tools_auth else "",
            })
        elif not self._is_authenticated():
            self._send_json(401, {"error": "Sign in to access the local dashboard."})
        elif self.path == "/api/log":
            self._send(200, _get_log_json().encode("utf-8"), "application/json")
        elif self.path == "/api/history":
            self._send(200, json.dumps(_history_summary()).encode("utf-8"), "application/json")
        elif self.path.startswith("/api/history/"):
            sid = urllib.parse.unquote(self.path[len("/api/history/"):])
            session = _history_session(sid)
            self._send(200, json.dumps(session or {}).encode("utf-8"), "application/json")
        elif self.path == "/api/artifacts":
            self._send(200, json.dumps(list_artifacts()).encode("utf-8"), "application/json")
        elif self.path.startswith("/artifact/"):
            self._serve_artifact(self.path[len("/artifact/"):])
        elif self.path == "/api/sandbox":
            self._send(200, _get_sandbox_json().encode("utf-8"), "application/json")
        elif self.path == "/api/subagents":
            self._send(200, json.dumps(list_subagents_data()).encode("utf-8"), "application/json")
        elif self.path == "/api/settings":
            self._send(200, json.dumps({
              "groups": SETTING_GROUPS, "values": get_masked_settings(),
              "agents": get_agent_providers(),
            }).encode("utf-8"), "application/json")
        else:
            self._send(404, b"Not found", "text/plain")

    def _serve_artifact(self, artifact_id):
        artifact_id = urllib.parse.unquote(artifact_id)
        path = get_artifact_path(artifact_id)
        if not path or not os.path.isfile(path):
            self._send(404, b"Artifact not found", "text/plain")
            return
        with open(path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(path)[1].lower()
        content_type = "application/pdf" if ext == ".pdf" else "text/markdown; charset=utf-8" if ext == ".md" else "application/octet-stream"
        self._send(200, data, content_type, {"Content-Disposition": f'attachment; filename="{os.path.basename(path)}"'})

    # --- POST ---
    def do_POST(self):
        auth_routes = {"/api/auth/signup", "/api/auth/login", "/api/auth/logout"}
        if not self._origin_is_local():
            self._send_json(403, {"error": "Cross-origin requests are not allowed."})
            return
        if self.path not in auth_routes and not self._is_authenticated():
            self._send_json(401, {"error": "Sign in to access the local dashboard."})
            return
        if self.path in ("/api/auth/signup", "/api/auth/login"):
            if not tools_auth:
                self._send_json(503, {"error": "Local account support is unavailable."})
                return
            body = self._read_json_body()
            try:
                if self.path.endswith("signup"):
                    token, account = tools_auth.signup(
                      body.get("name", ""), body.get("identifier", ""), body.get("password", ""))
                    if tools_profile:
                        tools_profile.set_name(account["name"])
                else:
                    token, account = tools_auth.login(body.get("identifier", ""), body.get("password", ""))
                authenticated_event.set()
                cookie = f"agatha_session={token}; Path=/; HttpOnly; SameSite=Strict"
                if body.get("remember", True):
                  cookie += f"; Max-Age={tools_auth.SESSION_TTL_SECONDS}"
                self._send_json(200, {"ok": True, "account": account}, {"Set-Cookie": cookie})
            except tools_auth.AuthenticationError as error:
                self._send_json(400, {"ok": False, "error": str(error)})
            except OSError as error:
                self._send_json(500, {"ok": False, "error": f"Could not save the local account: {error}"})
        elif self.path == "/api/account/profile":
            if not tools_auth:
                self._send_json(503, {"error": "Local account support is unavailable."})
                return
            body = self._read_json_body()
            try:
                account = tools_auth.update_profile(body.get("name", ""), body.get("identifier", ""))
                if tools_profile:
                    tools_profile.set_name(account["name"])
                self._send_json(200, {"ok": True, "account": account})
            except tools_auth.AuthenticationError as error:
                self._send_json(400, {"ok": False, "error": str(error)})
            except OSError as error:
                self._send_json(500, {"ok": False, "error": f"Could not update your profile: {error}"})
        elif self.path == "/api/account/password":
            if not tools_auth:
                self._send_json(503, {"error": "Local account support is unavailable."})
                return
            body = self._read_json_body()
            try:
                message = tools_auth.change_password(
                    body.get("current_password", ""), body.get("new_password", ""))
                self._send_json(200, {"ok": True, "message": message})
            except tools_auth.AuthenticationError as error:
                self._send_json(400, {"ok": False, "error": str(error)})
            except OSError as error:
                self._send_json(500, {"ok": False, "error": f"Could not update your password: {error}"})
        elif self.path == "/api/auth/logout":
            if tools_auth:
                tools_auth.logout(self._session_token())
            authenticated_event.clear()
            cookie = "agatha_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
            self._send_json(200, {"ok": True}, {"Set-Cookie": cookie})
        elif self.path == "/api/send":
            content_type = self.headers.get("Content-Type", "")
            try:
                if content_type.startswith("multipart/form-data"):
                    if parse_multipart_form is None:
                        raise ValueError("File uploads are unavailable.")
                    text, attachments = parse_multipart_form(content_type, self._read_request_body())
                    if text or attachments:
                        submit_user_message({"text": text, "attachments": attachments})
                else:
                    body = self._read_json_body()
                    text = (body.get("text") or "").strip()
                    if text:
                        submit_user_message(text)
            except (ValueError, OSError) as error:
                response = json.dumps({"ok": False, "error": str(error)}).encode("utf-8")
                self._send(400, response, "application/json")
                return
            self._send(200, b'{"ok":true}', "application/json")
        elif self.path == "/api/new_chat":
            start_new_session()
            new_chat_event.set()
            self._send(200, b'{"ok":true}', "application/json")
        elif self.path == "/api/resume_chat":
            body = self._read_json_body()
            session_id = str(body.get("id") or "")
            if resume_session(session_id):
                self._send(200, b'{"ok":true}', "application/json")
            else:
                self._send(404, b'{"ok":false}', "application/json")
        elif self.path == "/api/settings":
            body = self._read_json_body()
            key, value = body.get("key"), body.get("value", "")
            try:
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ValueError("A setting key and value are required.")
                message = set_setting(key, value)
                masked = get_masked_settings().get(key, {}).get("masked", "")
                self._send_json(200, {"ok": True, "message": message,
                            "masked": masked})
            except (ValueError, OSError) as error:
                self._send_json(400, {"ok": False, "error": str(error)})
        else:
            self._send(404, b"Not found", "text/plain")


def start_dashboard(port: int = DASHBOARD_PORT):
    """Start the dashboard HTTP server in a background daemon thread.
    Returns False on a port conflict so the agent can refuse duplicate startup."""
    global _server_thread
    if _server_thread is not None:
        return True

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError as e:
        print(f"[Warning] Dashboard could not bind to port {port} ({e}). "
              "Stop the other instance or set DASHBOARD_PORT in .env.")
        return False

    def _run():
        print(f"[Info] Dashboard available at: http://localhost:{port}")
        try:
            server.serve_forever()
        except Exception as e:
            print(f"[Warning] Dashboard server stopped: {e}")

    _server_thread = threading.Thread(target=_run, daemon=True)
    _server_thread.start()
    return True
