# Jev Agent Desk

A local web interface for talking to Codex and Claude conversations running in
tmux. Type or dictate into one message box, navigate conversations, read replies,
ask about a session, and interrupt a running turn. Jev (TypeSafe) interprets the
request; Python validates the selected conversation and performs the action.

By default the message box talks to **Jarvis**, one continuing conversation owned by
the app that sits over your existing agent chats. Jarvis can list and read chats,
deliver your exact words to one, interrupt one, and queue a quiet explanation of
another's output. A "Send to" selector switches to **Selected pane (direct
dictation)**, which delivers to the selected pane as before. See
[Jarvis conversation](#jarvis-conversation).

Completed agent replies are automatically prepared as complete spoken renditions.
Readback offers the prepared rendition or exact original wording. The original
remains visible. Stop & listen cancels readback independently of the agent's work;
uncertain delivery never triggers an automatic resend. Submitted input means the
text was handed to the chat, not that its task is complete.

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
| `TYPESAFE_API_KEY` | TypeSafe Jev request interpretation |
| `ELEVENLABS_API_KEY` | ElevenLabs realtime recognition and speech synthesis |
| `ELEVENLABS_VOICE_ID` | A voice accessible to that ElevenLabs account |
| `JEV_ASK_MODEL` | Model for session questions, indirect-question grammar, and spoken renditions; default `claude-haiku-4-5-20251001` through Claude CLI. Non-Claude model names use Codex. |
| `JEV_ASK_EFFORT` | Codex reasoning effort when a Codex model is selected; default `none`. Claude calls use low effort with thinking disabled. |
| `JEV_SETTINGS_FILE` | Optional explicit path to a private settings file; no file is loaded by default |

`python launch.py start --ask-key` prompts privately for just the TypeSafe key.
An optional settings file accepts `NAME=value` or `export NAME=value` for the three
TypeSafe/ElevenLabs variables only; existing environment values win. Keep that file
outside the checkout with owner-only permissions. Do not commit credentials.

Jarvis turns need both a Jev key (for the focused judgments listed below) and an
authenticated Claude Code CLI (each turn is a fresh Haiku 4.5 call). Without a Jev
key the interface still works for manual controls and direct selection, but Jarvis
cannot authorize a send, so none is made.

Background preparation uses the configured model and its existing CLI account
allowance, even while voice is off. Replies over 20,000 characters are not prepared.
Fresh or unfinished replies wait for completion; unidentified panes are skipped.

Generated answers and spoken renditions default to Haiku with thinking disabled,
using an authenticated Claude Code CLI subscription. Each call uses a fresh temporary
session with tools, MCP, hooks and session persistence disabled. Provider failure is
reported without switching models or authentication routes. Selecting a non-Claude
`JEV_ASK_MODEL` explicitly uses the isolated Codex path instead.
Native Codex sessions require an installed, authenticated Codex CLI. Claude sessions
require an installed, authenticated Claude Code CLI. Authenticate those CLIs separately; this app does
not provision accounts. Voice requires ElevenLabs access to `scribe_v2_realtime`
and `eleven_flash_v2_5` (or the optional `eleven_multilingual_v2` voice profile).

Compatibility baseline observed during export: Codex CLI `0.159.2`, Claude Code
`2.1.283`, tmux `3.4`, Python `3.13.12`, and websockets `16.0`. The app uses Codex
app-server history and CLI flags, terminal title/writer-lock identity, and Claude
lifecycle hooks; other CLI versions need validation. Provider/model access is
account-dependent. Automated tests use synthetic provider responses.

For a systemd deployment, ensure the service's `PATH` resolves the same Codex
installation you tested in the terminal. An older npm-installed CLI can shadow
the standalone CLI in `~/.local/bin`. With `gpt-6-luna`, CLI `0.145.0`
returned a ChatGPT model-support error; the same request succeeded on `0.159.3`.
Restart the service after correcting its `PATH`.

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
in-process. This is a controlled restart, not process crash recovery. A crash can
leave an owner record: verify that its process is gone before removing that stale
record. The Jarvis ledger in the run directory does survive a restart or crash (see
[Runtime store and recovery](#runtime-store-and-recovery)); legacy direct-mode
pending requests and pane selection are not restored.

## Jarvis conversation

Jarvis is the default target of the message box. Each turn runs a fresh, isolated
Claude Haiku 4.5 call through your Claude CLI subscription. The call has no
built-in tools, no hooks, no session persistence, thinking disabled and provider
API overrides removed. Its only tools are the app's own, reached through a private
loopback bridge, with at most 8 tool calls and a 60 s limit per turn. The app, not
the model, owns the conversation state; Haiku receives a bounded snapshot (24
recent turns bounded to 32 KiB, 8 KiB older continuity entries, 8 KiB pending
unclassified user sources, 8 KiB evidence excerpts, 64 KiB total).

What Jarvis can do: list and read chats, set the conversational subject, switch the
visible pane, deliver a message to a chat, interrupt a chat, and queue a background
explanation of a chat's output. Things to know:

- **The subject is not the selected pane.** "It", "that" and "those" refer to the
  app's conversational subject, which changes only when a tool resolves a chat.
  Switching the visible pane does not change it. With no subject, Jarvis asks which
  chat you mean rather than falling back to the selected pane.
- **Exact words and recipient.** Delivery uses your wording, cut from your own
  message at boundaries the app offers; the model only picks an offered boundary
  and never supplies message text. The only generated text is the
  indirect-question rewording the existing delivery path already uses.
  Recipient identity is captured and checked again at send. A replaced conversation
  blocks a follow-up even if it has the same name and pane. Jarvis retains the exact
  intended body and asks you to confirm the offered current conversation; another
  replacement requires another confirmation. Confirming a read or subject resolution
  never turns that command into a message.
- **Jev authorizes effects.** Before a send or interrupt, Jev answers a compact
  authorization question about your current message alone: permission, recipient,
  pane, and where the message starts and ends. The app compares the target and exact
  text to that answer. Discussion, negation, questions and anything that appears
  only in agent replies do not authorize an effect. Jev answers are model
  judgments; this is a safety check, not a proof.
- **Spoken results come from receipts.** Action outcomes are fixed phrases: "Input
  submitted to X. Waiting for its reply; its task is not complete.", "Delivery to X
  is uncertain. It will not be retried automatically.", or "No message was
  submitted." plus the reason. A reply from Haiku that claims no action passes a
  model-based narrative check or is replaced by the tool output. That check is a
  safety judgment, not deterministic proof that the sentence is true.
- **Preparation is not delivery.** If a turn prepares exact words but does not send,
  Jarvis says no message was submitted and offers the retained draft for explicit
  confirmation. Earlier readback stays available alongside that status.
- **One delivery attempt per user turn.** A changed draft, recipient or retry in the
  same turn is rejected. After an unconfirmed delivery you must decide again.
- **Originals stay available.** The exact original reply is always shown and can be
  read back; a spoken rendition is optional and bound to the exact app-confirmed
  reply text.
- **Stopping audio is not interrupting the agent.** Stop & listen cancels
  preparation and playback. Interrupting a chat is a separate action that needs
  its own authorization. A result that arrives after you stopped cannot restart
  audio.
- **Playback ledger.** Generated, scheduled and played are tracked separately per
  client and epoch. Only a full completion with its boundary marks a reply heard.
  A partial heard prefix is a count of Unicode characters with a hash and is only
  reported when the provider's alignment supports it. A stale or superseded client
  is rejected. When another device takes over playback, this client stops its own
  audio for that exact record, without touching newer local playback or the agent.
  See [VERIFICATION.md](VERIFICATION.md) for how this was exercised.
- **Quiet jobs.** An explanation queued for another chat runs as a background job
  (two at a time). Its completion is a quiet notice that you pick up by asking.
  Notices do not interrupt speech.
- **Unsent drafts and pending confirmations** stay visible and can be reviewed,
  confirmed or discarded by asking. A bare "yes" confirms a draft only when
  Jarvis offered that confirmation in the immediately preceding turn.

Endpoints, all loopback, Origin-checked, requiring the current `session_id` and
rejecting unknown fields: `GET /api/jarvis/state`, `POST /api/jarvis/turn`
(optionally a streamed NDJSON result), `POST /api/jarvis/turn-decision`,
`POST /api/jarvis/cancel` (exactly one of `turn_id` or `request_id`) and
`POST /api/jarvis/playback`. A repeated `request_id` returns the original outcome
and never repeats the effect. The legacy `/api/request` stays the direct-dictation
path. See [architecture](ARCHITECTURE.md).

### Runtime store and recovery

The Jarvis ledger is a SQLite database at `<run-dir>/jarvis.sqlite3` (WAL mode,
`synchronous=FULL`). It holds turns, requests, drafts, actions, jobs, playbacks,
clarifications, evidence excerpts, notices, decisions, the continuity summary and
the attention queue. It contains the exact text of your messages and excerpts of
agent replies, so treat it like `.run/` and keep it private.

The delivery attempt is written to the store before the external input is sent. A
checkpoint failure stops the external call rather than risking an unrecorded one.
On startup:

- an attempt that was in flight becomes **uncertain**; it is never replayed
- an unsent prepared draft or open clarification becomes **needs review**
- a turn that was still running is cancelled and marked interrupted, with canonical
  text such as "Input submitted to X. Its task is not complete; no automatic retry."
- a queued or running job becomes interrupted; no worker is resumed
- audio that was generating or playing becomes cancelled; nothing is inferred to
  have been heard, though a stored full-completion stays heard
- a repeated POST with a request ID the store knows returns the stored outcome
  without a second provider call or terminal input
- the browser page that sent the request and is still open reconnects, reads the
  stored turn and speaks that canonical text once, if the request is still its
  latest and you have not stopped readback. A page you reloaded, a request you
  superseded by switching to direct mode, and an ordinary cancellation stay quiet.
  The page never resends the request

Limits: this is store recovery, not process supervision or crash detection. If the
process dies between external input and the receipt write, the outcome is reported
uncertain. The text may in fact have been delivered. Read the target chat before you
send it again. Disk loss, a corrupt database, or running two server processes on one
run directory is unsupported.

### Isolated integration checkout

To exercise this build without touching a running service, use a separate checkout,
a private run directory, a distinct port, and a disposable tmux server that has no
existing conversations. Do not start it from a shell that has your production
credentials in the environment unless you intend it to use them, and do not use
`launch.py attach-start`, whose default reuses the credentials of a running sandbox.

```sh
git worktree add ../jev-agent-desk-integration <this-branch>   # or a fresh clone
cd ../jev-agent-desk-integration
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements.txt

RUN="$PWD/.run/integration"
install -d -m 700 "$RUN"
tmux -S "$RUN/tmux.sock" new-session -d -s main      # disposable, no existing chats

# Optional: Jev and voice settings from a private file outside the checkout
# (owner-only; lines of NAME=value for TYPESAFE_API_KEY, ELEVENLABS_API_KEY and
# ELEVENLABS_VOICE_ID). Never commit it or paste its values anywhere.
export JEV_SETTINGS_FILE=/path/outside/checkout/private-settings

python server.py --socket "$RUN/tmux.sock" --source-session main --view-session main \
  --independent-selection --redact-terminal-logs \
  --run-dir "$RUN" --port 54617 --voice-port 54618
```

The server prints its loopback URL and writes it to `$RUN/url`; it binds only
`127.0.0.1`. Choose ports that differ from your running service. Check it with
`curl http://127.0.0.1:54617/api/jarvis/state` and open the URL in a browser. Stop
the server with Ctrl-C, then remove only what you created:
`tmux -S "$RUN/tmux.sock" kill-server`. Restarting with the same `--run-dir` reopens
the same ledger and applies the recovery rules above; a new run directory starts an
empty one. Omit `JEV_SETTINGS_FILE` to start with no credentials, which is enough to
check startup and the state endpoint but not Jarvis turns.

This does not deploy anything. Replacing a running service with this build is a
separate, deliberate step that has not been performed and is not documented as
verified: it would use its own run directory (the ledger is not migrated from
elsewhere), requires a service restart, and should wait for the gates listed in
[VERIFICATION.md](VERIFICATION.md).

## Voice and mobile use

Mobile opens with the terminal filling the available space. **Tabs** opens or
collapses the left sidebar. Type and dictate into the same composer. **Send**
delivers ordinary messages to the selected agent and presses Enter automatically.
Jev routes explicit workspace commands separately. Drafts stay with their panes
when switching tabs. Options contains Enter and Escape for terminal interaction;
History and secondary controls stay collapsed until needed.
Terminal text scales down to fit each pane's available width, including after
resizing or opening the sidebar. Line breaks and column alignment stay intact;
scroll vertically for history. This does not resize the underlying tmux workspace.

Replies wait quietly. Ask **“What's next?”** for one recommended chat and its
reason, or **“What else is ready?”** for a rundown. Agents explicitly needing
your input come first, followed by unread completed replies in arrival order.
**“Go there”** selects the recommended pane; **“Read it”** selects it and reads
that reply, so your next answer goes to the conversation you just heard.
The recommendation retains its exact conversation and reply identity. If either
changes, ask again; it never silently substitutes another chat.

Only finished audio marks a reply heard. Stopping, failed audio, viewing the
original, and switching tabs leave it unread. A reply asking for a decision
stays pending after listening until you answer or the agent resumes. Jev judges
those requests when you ask for the queue; readiness is not proof a whole task
is complete. This works for replies originating in Jev or the terminal. The
queue and its heard state are checkpointed in the Jarvis store and restored on
restart; a reply is heard only if its full-completion acknowledgment was stored.
There are no closed-app alerts.

Allow microphone access and wait for Listening. Speech and typing share a draft
bound to the conversation where composition began. When to send depends on the
mode:

- **Jarvis (default):** about 0.7 s after a committed transcript, the server asks
  Jev one focused question: is this utterance complete, still unfinished, or
  cancelled? Only a *complete* judgment for that exact transcript text and revision
  sends. *Wait* holds the draft; the app rechecks after 4 s, then again 8 s later,
and then stops (at most two rechecks) until you add words or press Send. New words
  invalidate older judgments, so a late "complete" cannot send stale text. If Jev
  fails, the text is held and never sent. A *cancel* judgment discards the draft
  locally and says "Nothing was sent. Your words are saved." Send now bypasses the
  wait; Hold keeps the draft.
- **Direct dictation:** the previous behavior, a fixed 3 s of silence before send.

Switching panes or changing identity holds the draft until you return or explicitly
retarget it.

Replies to Jev-sent messages appear with their originating request. Readback uses
the prepared rendition by default; Original wording bypasses rewriting. Rendering
preserves already-readable prose and converts visual structure into spoken sentences.
Provider failure leaves the original visible. Generated text streams into speech
in short phrases, and audio plays as it arrives. A phrase is up to six complete
words, or ends at punctuation followed by whitespace, whichever comes first.
Partial words wait for a boundary; the final tail is flushed when generation ends.
Punctuation inside numbers, identifiers and URLs does not split a phrase.
A background watcher checks
all identified agent panes, pausing three seconds between scans, independently of
the browser and voice toggle. It prepares the latest completed reply on attachment and each new
completed reply, including terminal-originated conversations. Jev is not involved
in preparation. The latest rendition for each attached session stays in memory;
preparation never starts playback. Original wording remains available.
Fresh explanations still wait for the model's first text and a complete phrase; streaming
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

Codex history retrieval also supports the paginated history contract verified with
CLI `0.159.3`, retaining complete turn items for readback and reply tracking.

For an optional live Jev routing evaluation with synthetic tabs, run
`python eval_routing.py` with `TYPESAFE_API_KEY` configured. It makes paid inference
calls but does not access terminals. Its cases exercise routing judgments;
passing them is not a guarantee for every phrasing.

`probe_jarvis_haiku.py` and `probe_jarvis_scenario.py` make real Haiku calls with
synthetic application tools; they spend subscription allowance and are not part of
`check.py`.

See [verification](VERIFICATION.md) for what was measured, what is synthetic, what
is still open, and what is unverified (including every physical-phone property).

## Data and licensing

`.run/` can contain messages, terminal captures, transcripts, process bindings,
the Jarvis ledger (`jarvis.sqlite3`) and provider diagnostics. Treat it as private.
Requests and focused judgment questions (your current message, tab names and pane
descriptions) go to TypeSafe; microphone audio and spoken text go to ElevenLabs;
Jarvis turns, session questions and renditions use your configured Claude or Codex
CLI provider, and a Jarvis turn sends it your current message plus the bounded
conversation snapshot and any tool output it reads. Review those data flows before
using sensitive conversations.

This repository begins with a clean source export, without private workspace
history or runtime data. No software license has been selected for the exported
source. Licensing remains for the owner to decide. Dependencies retain their respective
licenses.
