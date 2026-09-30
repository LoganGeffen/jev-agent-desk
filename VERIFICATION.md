# Export verification

The exported application passed 116 Python tests, 39 JavaScript tests, and the
intercepted Chromium conversation scenario on Linux/WSL2 with Python 3.13.12,
Node.js 24.13.1, tmux 3.4, websockets 16.0 and Playwright 1.60.0.

A separate clone was installed with `python3 -m venv .venv` and
`python -m pip install -r requirements-dev.txt`. `python check.py` passed the same
116 Python tests, 39 JavaScript tests and browser scenario from that checkout.
Credential-free `launch.py start`, HTTP state/static assets, `status`, and `stop`
also passed with four disposable fixture tabs and both providers unconfigured.
No files from the original workspace are required by that checkout.

The Python suite exercises disposable tmux lifecycle, explicit pane identity,
message extraction/delivery, independent mobile selection, Host/Origin checks,
voice configuration, provider failures, and original/rendition choice. Deliberately
rejected WebSocket origins can produce expected handshake diagnostics on stderr.

JavaScript tests exercise drafts, exact request payloads, uncertain delivery,
poll recovery, microphone/recognition simulations, readback mode caches, stopping
preparation/playback, and rejection of late results. The browser scenario checks
the shared typed/spoken path, cross-agent focus, held drafts after navigation or
identity changes, background original replies, explicit raw keys, automatic-mode
default, and mobile viewport overflow.

These tests use synthetic providers and intercepted browser endpoints. They do not
establish live model accuracy, current account/model access, physical microphone
quality, phone speaker quality, mobile networking, or background behavior. Native
agent conversations and paid inference were not exercised during packaging.

The export contains selected app modules, their required session identity module,
synthetic tests and new developer documentation. Runtime directories, credentials,
real conversation records, machine-specific operational notes and parent Git
history are excluded. Gitleaks 8.30.1 found no credential patterns in the staged
source export and its single-commit history; this scan is supplementary to the
file-list and privacy review. Core request, control, browser, readback and identity
modules match the source implementation. Packaging changes make the identity
dependency local, require an explicit attachment socket, load settings only by
explicit path, and use Playwright-managed Chromium for browser tests.
