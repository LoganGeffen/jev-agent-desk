# Jev Agent Desk

A local web interface for talking to Codex and Claude conversations running in
tmux. Type or dictate into one message box, navigate conversations, read replies,
ask about a session, and interrupt a running turn. Jev (TypeSafe) interprets the
request; Python validates the selected conversation and performs the action.

Automatic readback lets Jev choose between the exact original reply and a complete
spoken rendition. Original wording and conversational rendition are also explicit
choices. The original remains visible. Stop & listen cancels readback independently
of the agent's work; uncertain delivery never triggers an automatic resend.

This is a developer prototype for **Linux or WSL2**, using Linux `/proc` and tmux.
It is a local control service, not a hosted multi-user application. Anyone with
access to its endpoint can interact with the attached terminals.

## Install and try the interface

Requirements: Python 3.11+, tmux, and a browser. On Debian/Ubuntu:

```sh
sudo apt-get install python3-venv tmux
git clone https://github.com/LoganGeffen/jev-agent-desk.git
cd jev-agent-desk
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python launch.py start
```

Attaching to daemon-backed Codex terminals also uses `ss` (the `iproute2` package)
to identify the connected conversation without changing Codex settings.

Open the loopback URL printed by the launcher. This starts a dedicated tmux server
with four clock fixtures. Without credentials you can browse the interface and use
manual controls; natural-language routing and voice are unavailable. It does not
attach to existing conversations.

```sh
python launch.py status
python launch.py stop
```

`stop` terminates this sandbox's entire tmux server, including any agents you added
to it. Logs stay in the ignored `.run/` directory.

## Enable Jev, voice, and agent replies

Set configuration in the environment **before starting** the app:

| Variable | Used for |
| --- | --- |
| `TYPESAFE_API_KEY` | TypeSafe Jev request interpretation and automatic readback choice |
| `ELEVENLABS_API_KEY` | ElevenLabs realtime recognition and speech synthesis |
| `ELEVENLABS_VOICE_ID` | A voice accessible to that ElevenLabs account |
| `JEV_ASK_MODEL` | Codex model for session questions, indirect-question grammar, and spoken renditions; default `gpt-6-luna` |
| `JEV_ASK_EFFORT` | Reasoning effort for those calls; default `none`. Must be supported by the selected model. |
| `JEV_SETTINGS_FILE` | Optional explicit path to a private settings file; no file is loaded by default |

`python launch.py start --ask-key` prompts privately for just the TypeSafe key.
An optional settings file accepts `NAME=value` or `export NAME=value` for the three
TypeSafe/ElevenLabs variables only; existing environment values win. Keep that file
outside the checkout with owner-only permissions. Do not commit credentials.

Native Codex sessions and text generation require an installed, authenticated
Codex CLI and access to the selected model. Claude sessions require an installed,
authenticated Claude Code CLI. Authenticate those CLIs separately; this app does
not provision accounts. Voice requires ElevenLabs access to `scribe_v2_realtime`
and `eleven_flash_v2_5` (or the optional `eleven_multilingual_v2` voice profile).

Compatibility baseline observed during export: Codex CLI `0.159.2`, Claude Code
`2.1.283`, tmux `3.4`, Python `3.13.12`, and websockets `16.0`. The app uses Codex
app-server history and CLI flags, terminal title/writer-lock identity, and Claude
lifecycle hooks; other CLI versions need validation. Provider/model access is
account-dependent. Automated tests use synthetic provider responses.

With the sandbox running, optional disposable agent windows can be added:

```sh
python launch.py add-codex Luna
python launch.py add-claude Claude
```

These launch restricted test conversations. The Codex helper expects its trust
prompt and `/status` UI; startup/update prompts can change between CLI versions.
If startup times out, inspect the new pane using the printed tmux command rather
than repeatedly launching more agents. The Claude helper supplies identity hooks
and uses Haiku with tools disabled.

## Attach to your tmux workspace

Inside the tmux server you want to use, obtain its explicit socket path and session:

```sh
tmux display-message -p '#{socket_path}'
tmux display-message -p '#{session_name}'
```

Then, from this checkout with its virtual environment active:

```sh
python launch.py attach-start \
  --socket /path/printed/by/tmux \
  --source-session main --view-session main --independent-selection \
  --run-dir .run/attached
python launch.py attach-status --run-dir .run/attached
python launch.py attach-stop --run-dir .run/attached
```

Replace the socket and `main` with your values. Independent selection keeps web
navigation from changing desktop tmux focus or geometry. All clients of one web
process share its selected pane. Send, raw terminal input, interrupt, and confirmed
close act on real terminals. `attach-stop` stops only the web process.

Codex identity resolves from a live writer lock, with terminal-title and metadata
disambiguation. Starting Codex with `-c 'tui.terminal_title=["session-id"]'` helps
identify the active conversation. Ambiguous or replaced identities block agent
actions. Existing Claude panes need lifecycle bindings in this app's run directory;
unhooked Claude panes remain available through manual terminal controls. See
[architecture](ARCHITECTURE.md) for the hook contract.

`attach-restart --run-dir .run/attached` validates the recorded process identity and
retains its configured ports, origin, selection mode, and service credentials
in-process. This is a controlled restart, not crash recovery. A crash can leave an
owner record: verify that its process is gone before removing that stale record.
Pending requests and selection are not restored across restarts.

## Voice and mobile use

Mobile opens with the terminal filling the available space. **Tabs** opens or
collapses the left sidebar. Type and dictate into the same composer. **Send**
delivers ordinary messages to the selected agent and presses Enter automatically.
Jev routes explicit workspace commands separately. Drafts stay with their panes
when switching tabs. Options contains Enter and Escape for terminal interaction;
History and secondary controls stay collapsed until needed.

Allow microphone access and wait for Listening. Speech and typing share a draft
bound to the conversation where composition began. Speech sends after roughly
four seconds of silence; Hold and Send now control that behavior. Switching panes
or changing identity holds the draft until you return or explicitly retarget it.

Replies appear with their originating request. Automatic readback preserves prose
when Jev chooses original, and rewrites visual structure when it chooses rendition.
This choice is probabilistic. Uncertainty or provider failure leaves the original
visible without substitute speech or retries. Generated text streams into speech
sentence by sentence, and audio plays as it arrives. Completed replies tracked by
the app start preparing their readback in the background. Original wording remains
available. Fresh explanations still wait for the model's first sentence; streaming
does not make that initial inference instant. Stop cancels queued audio and ignores
late results; server preparation already in progress may finish.

Jev first separates agent messages, inspection of existing output, and workspace
control. Inspection then chooses readback or an observer explanation; that branch
cannot send or interrupt. “Tell me what Beta is doing” observes its evidence;
“Ask Beta what it is doing” sends Beta a message. Observer answers go straight to
speech without another rendition pass.

Recognition pauses during playback by default. Use Stop & listen, or enable Talk
to interrupt with headphones. Hiding the page stops voice; enable it again on
return. Physical-phone acoustics and background reliability are not established
by the browser tests.

For a phone, use a **private authenticated HTTPS reverse proxy** (for example,
Tailscale Serve on your tailnet). The app has no user authentication and must not
be directly exposed to the public internet. A configured origin is an origin
check, not access control. Start with fixed loopback ports:

```sh
python launch.py attach-start \
  --socket /path/printed/by/tmux \
  --source-session main --view-session main --independent-selection \
  --run-dir .run/mobile --port 54534 --voice-port 54535 \
  --public-origin https://your-private-host.example
```

Configure your private proxy to forward `/` to `http://127.0.0.1:54534` and
WebSocket `/voice` to `http://127.0.0.1:54535`. The exact HTTPS origin must match
`--public-origin`, including a nonstandard port. Both services bind to loopback.

## Tests

Node.js 20+ is needed for JavaScript tests. Install Chromium for the intercepted
browser scenario:

```sh
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python check.py
```

On minimal Linux hosts, `python -m playwright install --with-deps chromium` can
also install browser system dependencies (requires package-manager privileges).
The test runner removes service configuration from its child environment. Tests
use disposable tmux sockets, synthetic identities, mocked providers, and an
intercepted browser page; they do not send to existing agent conversations or make
paid inference calls. Browser artifacts are generated under `.run/`.

For an optional live Jev routing evaluation with synthetic tabs, run
`python eval_routing.py` with `TYPESAFE_API_KEY` configured. It makes paid inference
calls but does not access terminals. Its cases exercise routing judgments;
passing them is not a guarantee for every phrasing.

See [verification](VERIFICATION.md) for the export checks and their limits.

## Data and licensing

`.run/` can contain messages, terminal captures, transcripts, process bindings and
provider diagnostics. Treat it as private. Requests go to TypeSafe; microphone
audio and spoken text go to ElevenLabs; session questions and renditions use your
authenticated Codex provider. Review those data flows before using sensitive
conversations.

This repository begins with a clean source export, without private workspace
history or runtime data. No software license has been selected for the exported
source. Licensing remains for the owner to decide. Dependencies retain their respective
licenses.
