# Agatha AI

Agatha is a local Python assistant with an optional browser dashboard. It can use a local Ollama server or configured LLM providers and exposes local capabilities only to the user running the process.

## Requirements

- Python 3.10 or newer
- macOS, Windows, or Linux for the core text agent
- Ollama is optional; install it separately if you want a local model
- Microphone, speech, and individual native-app integrations are optional

## Local Setup

Create the project environment and install dependencies:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

On macOS/Linux, start Agatha with:

```sh
bash run_local.sh
```

The launcher checks the configured Ollama endpoint first. It reuses an already-running server, or starts one local `ollama serve` process if available. If Ollama is unavailable, the agent continues to configured fallbacks. The model must be downloaded separately, for example `ollama pull qwen2.5:1.5b`.

Microphone/speech support is optional: install `requirements-voice.txt`. The offline Hugging Face fallback is large and optional: install `requirements-local-llm.txt`.

Beat analysis is included in `requirements.txt`. Editing a video URL and synchronizing clips to a song uses `yt-dlp`, `librosa`, and FFmpeg; it does not require an OpenAI key. For free local prompt-to-video generation, install ComfyUI, install a compatible open-source video model/workflow, export its workflow in API format, and set `AGATHA_COMFYUI_WORKFLOW` to that JSON file. Agatha submits `{{PROMPT}}` and `{{DURATION}}` placeholders to ComfyUI, downloads the generated footage, and can cut it to a supplied song's detected beats. ComfyUI runs locally and requires no cloud API key, but video models need substantial RAM/GPU memory and generation time. The existing still-image slideshow generator remains available when an image-generation provider is configured. FFmpeg is required for rendering and beat-synced cuts (`brew install ffmpeg` on macOS).

The dashboard is available at `http://127.0.0.1:8765`. On first use, register a local owner account with a preferred name, username or email, and password of at least 10 characters. Agatha waits to speak or listen until a dashboard session is authenticated. “Keep me signed in” stores a signed, revocable session cookie for up to 30 days, never the password; signing out revokes remembered sessions. The account and profile are stored on this device and are not a multi-user isolation boundary. The dashboard intentionally listens only on loopback and must not be exposed to the LAN or internet.

Settings credentials are entered and saved individually. Social integrations generally require platform-issued app passwords, API tokens, or OAuth credentials rather than a platform account password; Agatha does not collect or submit third-party account passwords.

Settings also includes **Connect agents** for Claude, ChatGPT, Gemini, Grok, and Groq. Add a provider-issued API key and model ID; interactive consumer-account password login is not supported. When two or more providers are connected, Agatha can send relevant requests, drafts, and tool results to them for review before composing a final response. Provider terms, data handling, rate limits, and possible charges apply. Reviewers do not re-run actions; Agatha executes tools once and uses their returned results as completion evidence.

Chat attachments can include selectable-text PDFs, DOCX/ODT/RTF documents, PPTX/XLSX files, EPUB books, and common text/code formats. Ask Agatha to summarize an attached document or book. Scanned PDFs need OCR before upload. Extraction is local and capped at 2 million characters per file; long documents are summarized in bounded sections, and capped uploads are identified in the response.

On Windows, create `.venv` with `py -m venv .venv`, install with `.venv\Scripts\python.exe -m pip install -r requirements.txt`, then launch with `.venv\Scripts\python.exe main_agent.py`. Install Ollama separately and run one `ollama serve` instance; Agatha detects an existing endpoint and does not start another one.

## Local App Access

Operating systems own the permissions. Agatha cannot grant itself access or request a single blanket permission for every app and device.

- macOS: Calendar, Notes, Reminders, and Mail use Apple Events. Approve each first-use Automation prompt for the process launching Agatha. Messages requires the separate explicit opt-in and Full Disk Access described in [MACOS_INTEGRATIONS.md](MACOS_INTEGRATIONS.md).
- Windows: Calendar can use classic Outlook automation when Outlook and `pywin32` are installed; otherwise Agatha saves an `.ics` file for the user to import.
- Android: when run on-device in an environment with Android's `am` service, Calendar opens a prefilled system event form. The user reviews and saves it. If no system intent handler is available, Agatha creates an `.ics` file. For desktop-hosted Agatha, these intents target the desktop host, not a connected phone.
- Notes outside macOS are saved as local Markdown in `Documents/Agatha Notes` (Android shared storage is used only when already available).
- Timed reminders are prepared as alarm-bearing `.ics` files; they are not scheduled until imported into a calendar app. This avoids claiming success where the OS does not expose a reliable write API.

See [MACOS_INTEGRATIONS.md](MACOS_INTEGRATIONS.md) for macOS privacy setup. Android and Windows integrations require the relevant device/app APIs and user confirmation; there is no automatic cross-device permission grant.

## Hosting and Privacy

### Render Deployment

This repository includes a Render Blueprint in `render.yaml`. In Render, create a new Blueprint from the repository and configure these environment secrets before the first deploy:

- `AGATHA_OWNER_IDENTIFIER`: the username or email used to sign in.
- `AGATHA_OWNER_PASSWORD`: a unique password of at least 16 characters. The account is provisioned on first startup; public account creation is disabled in hosted mode.
- `OPENAI_API_KEY`: optional, but configure a hosted model provider if you want LLM chat and image generation. Other supported providers can be configured from the authenticated dashboard.

The Blueprint builds `Dockerfile`, installs the core Python packages from `requirements.txt` plus FFmpeg and ClamAV system packages, starts `render_start.py`, binds the dashboard to Render's `PORT`, exposes `/healthz` for health checks, and mounts a 1 GB persistent disk at `/var/data`. ClamAV signature refresh starts in the background and does not delay web startup. The disk is required to retain the owner account, sessions, chat history, settings, uploads, artifacts, and other app data across deploys. The Starter web service and persistent disk are paid Render resources; check current Render pricing before deploying.

Do not add `requirements-local-llm.txt` unless you intend to build and host the large Torch/Transformers stack. `requirements-voice.txt` is for local microphones/TTS and is intentionally not installed on Render. `yt-dlp` is included in the core requirements for creator URL ingestion; FFmpeg and ClamAV are installed by the Docker image rather than pip.

Hosted mode uses the dashboard and cloud-capable tools, but the server cannot control the user's physical device: microphone/speaker, Music.app/Spotify, macOS Calendar/Notes/Mail, or local folders on the user's computer. Those require Agatha to run locally or a separately installed companion app. Features that require local device APIs are not made remotely available by deploying this service.

The normal local dashboard still binds only to loopback. Do not set `DASHBOARD_HOST=0.0.0.0` outside the hosted configuration; hosted mode adds secure cookies, same-origin POST checks, a pre-provisioned owner, and disables public signup.

Keep API tokens and `.env` out of source control. Runtime histories and local task data are ignored by `.gitignore`.

## Tests

Run the local, non-destructive integration tests with:

```sh
.venv/bin/python -m unittest discover -s tests
```

These mock writes to personal apps. A successful mocked test does not prove that macOS Automation permission is granted; the first real use asks for that app-specific permission.
