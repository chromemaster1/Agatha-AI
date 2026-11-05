#!/bin/bash
# Always installs into THIS project's .venv, no matter which python/pip your
# current shell happens to resolve to. Run this from anywhere:
#   ./install_deps.sh

set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$DIR/.venv/bin/python"

if [ ! -f "$VENV_PY" ]; then
    echo "No venv found at $DIR/.venv — create one first:"
    echo "  python3 -m venv \"$DIR/.venv\""
    exit 1
fi

echo "Installing into: $VENV_PY"
echo ""
"$VENV_PY" -m pip install -r "$DIR/requirements.txt"

echo ""
echo "--- Verifying key packages landed in the right place ---"
"$VENV_PY" -c "
import importlib
checks = {
    'dotenv': 'python-dotenv', 'requests': 'requests', 'ddgs': 'ddgs', 'fpdf': 'fpdf2',
    'yfinance': 'yfinance',
}
import sys
if sys.platform == 'win32':
    checks['win32com.client'] = 'pywin32 (classic Outlook integration)'
for module_name, pip_name in checks.items():
    try:
        importlib.import_module(module_name)
        print(f'  OK       {pip_name}')
    except ImportError as e:
        print(f'  MISSING  {pip_name}  ({e})')
"
echo ""
echo "Run the agent the same explicit way:"
echo "  \"$VENV_PY\" \"$DIR/main_agent.py\""
echo "For the optional offline Hugging Face fallback, also install:"
echo "  \"$VENV_PY\" -m pip install -r \"$DIR/requirements-local-llm.txt\""
echo "For optional microphone and speech output, install:"
echo "  \"$VENV_PY\" -m pip install -r \"$DIR/requirements-voice.txt\""
