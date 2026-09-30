import os
from pathlib import Path
import subprocess
import sys


root = Path(__file__).resolve().parent
environment = {key: value for key, value in os.environ.items() if key not in {
    'TYPESAFE_API_KEY', 'ELEVENLABS_API_KEY', 'ELEVENLABS_VOICE_ID',
    'JEV_SETTINGS_FILE', 'JEV_ASK_MODEL', 'JEV_ASK_EFFORT', 'PYTHONPATH',
}}
commands = [
    [sys.executable, '-m', 'unittest', 'discover', '-p', 'test_*.py'],
    ['node', '--test', *[path.name for path in sorted(root.glob('test_*.mjs'))]],
    [sys.executable, 'test_conversation_browser.py'],
]
for command in commands:
    subprocess.run(command, cwd=root, env=environment, check=True)
