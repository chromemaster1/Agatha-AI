#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
OLLAMA_PID=""

if [[ ! -x "$PYTHON" ]]; then
    printf 'Missing project Python environment. Create it with: python3 -m venv "%s/.venv"\n' "$ROOT" >&2
    printf 'Then install dependencies with: "%s/.venv/bin/python" -m pip install -r "%s/requirements.txt"\n' "$ROOT" "$ROOT" >&2
    exit 1
fi

OLLAMA_URL="$(cd "$ROOT" && "$PYTHON" -c 'import os; from dotenv import load_dotenv; load_dotenv(); print(os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/"))')"

cleanup() {
    if [[ -n "$OLLAMA_PID" ]] && kill -0 "$OLLAMA_PID" 2>/dev/null; then
        kill "$OLLAMA_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

if ! curl --silent --fail "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    if [[ "$OLLAMA_URL" == http://127.0.0.1:11434 || "$OLLAMA_URL" == http://localhost:11434 ]] && command -v ollama >/dev/null 2>&1; then
        printf 'Ollama is not responding; starting one local server instance...\n'
        ollama serve >"$HOME/.agatha-ollama.log" 2>&1 &
        OLLAMA_PID=$!
        for _ in {1..20}; do
            if curl --silent --fail "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
                break
            fi
            if ! kill -0 "$OLLAMA_PID" 2>/dev/null; then
                OLLAMA_PID=""
                break
            fi
            sleep 0.5
        done
    fi
fi

if curl --silent --fail "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    printf 'Using Ollama at %s (existing server reused when already running).\n' "$OLLAMA_URL"
else
    printf 'Ollama is unavailable at %s; Agatha will use configured fallbacks.\n' "$OLLAMA_URL"
fi

cd "$ROOT"
"$PYTHON" main_agent.py
