# Jev Agent Desk

A local web interface for talking to Codex and Claude conversations running in
tmux. Type or dictate into one message box, navigate conversations, read replies,
ask about a session, and interrupt a running turn. Jev (TypeSafe) interprets the
request; Python validates the selected conversation and performs the action.

By default the message box talks to **Jarvis**, one continuing conversation owned by
the app that sits over your existing agent chats. Jarvis can list and read chats,
deliver your exact words to one, interrupt one, and queue a quiet explanation of
another's output. A **Jarvis / Pane** switch beside the microphone selects direct
dictation to the selected pane. See
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

- **The subject follows your explicit tab or pane selection.** "It", "that" and
  "those" refer to that exact conversation for the next fresh typed or voice
  request. Existing drafts stay with their original conversation; returning to it
  restores the draft. Polling and another device cannot change your selection.
  Named requests and successful Jarvis switches can establish a different subject.
  With no subject, Jarvis asks which
  chat you mean rather than falling back to the selected pane.
- **Exact words and recipient.** Exact dictation uses your wording, cut from your
  own message at boundaries the app offers. The controller model only picks an
  offered boundary and never supplies message text. Indirect questions can be
  reworded, and an explicitly requested composition can use an app-generated
  draft after fidelity checks; both retain the original source.
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
  model-based narrative check. A rejected status or summary gives a brief inability to verify, rather than reading an older reply as a fallback. That check is a
  safety judgment, not deterministic proof that the sentence is true.
- **Literal and composed messages.** Exact dictation and fully quoted messages use
  the original source bytes. When you ask Jarvis to lightly assemble a message,
  the app generates a draft from the full request and checks its meaning, details,
  grounding and references before sending. A generation or fidelity failure asks
  for exact wording and sends nothing. Old unresolved composition requests remain
  unsendable. Literal spans crossing a paired quote wrapper are rejected.
- **Current status differs from exact reading.** Current-status questions use a
  bounded live terminal and newest-turn snapshot in the same response. A completed
  earlier reply remains historical while newer work is active. Summaries and
  explanations keep their concise evidenced answer with the original separately
  inspectable; an explicit read keeps the original as the primary response.
- **Preparation is not delivery.** If a turn prepares exact words but does not send,
  Jarvis says no message was submitted and offers the retained draft for explicit
  confirmation. An incidental earlier original stays separately inspectable as
  text alongside that status, including after reload.
- **One delivery attempt per user turn.** A changed draft, recipient or retry in the
  same turn is rejected. After an unconfirmed delivery you must decide again.
- **Originals stay available.** A primary read response keeps the exact original
  and its original/spoken playback controls. An incidental original read during a
  send or prepared-unsent turn is shown separately as read-only text; it has no
  playback control. Automatic and manual playback for that turn use the canonical
  status, not the incidental original.
- **Spoken wording.** Optional renditions use citation labels rather than long
  incidental Markdown destinations, and natural numbers and units without rounding.
  Substantive commands, paths, names, values and uncertainties must retain their
  meaning. Original wording stays exact. Fixture tests exercise streamed preparation;
  physical pronunciation and model fidelity for every reply are not guaranteed.
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

Web Push state is separate at `<run-dir>/push.sqlite3`: device subscriptions,
per-conversation mutes, observed reply baselines, immutable notification events,
delivery results and open results. The VAPID private key is generated once at
`<run-dir>/vapid-private.pem` with owner-only permissions. Neither the private key
nor subscription endpoints appear in browser state. Reusing the run directory
retains both; losing it creates a new VAPID identity and invalidates old subscriptions.

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
Terminal text stays at 12 px and wraps wide lines, paths and identifiers inside
the pane, including when the sidebar is open. Original text, whitespace and
intentional line breaks remain intact; long rows continue on another display line.
Scroll vertically for available tmux scrollback. Polling preserves the reading
position and follows new output when already at the bottom. This does not resize
the underlying tmux workspace. Alternate-screen applications may keep older
conversation history internally; the terminal view can only show what tmux captures.

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

Optional Web Push alerts cover newly observed completed final replies, including
replies to turns started directly in the native terminal. Open **Notifications**,
then use **Enable**, **Test**, or **Disable** for this browser device. The test waits
two seconds so a phone can be locked. When enabled, **Mute this conversation on
this device** applies only to the selected provider conversation and subscription;
the mute remains if that same native conversation resumes in a new process.
Install the Desk from the browser or add it to the Home Screen for the normal
standalone mobile experience.

Badges and notification eligibility use current native lifecycle evidence. Codex
uses its own turn-start, turn-complete and interruption records; a separate
app-server's reconstructed history is not live activity. Claude uses its bound
lifecycle hooks checked against the latest transcript turn. **Reply ready** means
that turn has a final reply, not that the whole task is done. Missing or conflicting
evidence shows **status unknown**; an interruption shows **stopped** and a known
provider failure shows **error**. Old window status flags do not override this.
Required-input alerts are not inferred from quiet terminals or error messages.

Each push contains only the session label, the fixed body “Agent replied,” and an
opaque event plus exact conversation identity; reply and terminal text are never
sent to the browser vendor. Tapping resolves that immutable event after the page
loads. If the tab, pane or conversation identity changed, Desk shows an unavailable
message and does not substitute another conversation. Historical replies present
on first attachment, restart, native resume, or recovery from unavailable evidence
are primed silently. A newer active turn suppresses an older reply, and a reply
requires matching current native completion before it can notify. Event and delivery
records prevent replay across polling and server restart; an in-flight send at a
crash becomes uncertain and is not retried automatically. This notification/open
state does not mark audio heard.

The microphone button sits beside the **Jarvis / Pane** destination switch. In
Options, choose **Tap only** (the default), **Wake word**, or **Always on**. This
choice is stored on this device and applies to both destinations. Tap only starts
capture when you press the microphone button. Wake word automatically starts
local listening for **Hey Jarvis** when the app opens or returns to the foreground;
the visible status then says **Dormant · listening on-device for Hey Jarvis**.
Always on automatically opens continuous transcription while the app is visible.
The microphone button stops capture in any setting; an explicit stop holds it off
until the next app load or another tap. Background pages release the microphone.
Browser permission and audio activation still govern automatic startup. Safari may
ask for microphone permission again or require a tap to activate audio; the app
cannot grant or persist browser permission itself.

Say “Hey Jarvis, read Dog Data” or pause after the wake phrase, then speak your
request. A short local tone confirms wake. Continue with follow-up requests without
repeating the wake phrase. Recognition stays awake for sixty seconds of inactivity;
pending requests and response playback defer that timeout, giving another full
window after the response. It then closes recognition and returns to local waiting.
Wake alone sends no command. Saying Hey Jarvis during readback stops only audio;
ordinary background volume does not stop readback while waiting for wake.

Dormant listening opens no recognition connection. After wake, a bounded buffer of
about three seconds, including the wake phrase and possibly immediately preceding
sound, plus awake speech goes to ElevenLabs. Recognized words before the initial
wake boundary are excluded from the command. If transcription misses that boundary,
the request stays unsent. There is no browser speech-recognition or cloud wake
fallback. Model/runtime files are pinned and served from this app; see
[wake-assets/PROVENANCE.json](wake-assets/PROVENANCE.json) for licenses and rebuild.

A finalized voice draft paused solely by inactivity can continue after another
wake with its original recipient and Jarvis session/subject identity. A changed
subject or restarted Jarvis holds the exact words for review. Unfinalized, typed
and manually held drafts never merge silently into a new wake request. Switching
between **Jarvis** and **Pane** restarts active capture in the chosen activation
setting while keeping drafts in their original destination. Stop, page hiding and
microphone loss release capture; closed/background pages do not listen. Browsers
must support sixteen-kilohertz capture. Physical
phone acoustics, accents, noise and speaker echo are not established by the
synthetic-speaker Chromium tests.

Speech and typing share a draft bound to the conversation where composition began.
When to send depends on the mode:

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
That origin is also used as the VAPID contact subject. Web Push sends the bounded
notification payload through the browser vendor's push service; the first version
accepts production HTTPS endpoints for Apple Web Push, Firebase Cloud Messaging,
and Mozilla Push Service only.

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
the Jarvis ledger (`jarvis.sqlite3`), Web Push state (`push.sqlite3`), the VAPID
private key and provider diagnostics. Treat it as private.
Requests and focused judgment questions (your current message, tab names and pane
descriptions) go to TypeSafe; microphone audio and spoken text go to ElevenLabs;
Jarvis turns, session questions and renditions use your configured Claude or Codex
CLI provider, and a Jarvis turn sends it your current message plus the bounded
conversation snapshot and any tool output it reads. Review those data flows before
using sensitive conversations. When Web Push is enabled, a fixed notification
without reply text is sent to that subscription's browser-vendor endpoint.

This repository begins with a clean source export, without private workspace
history or runtime data. No software license has been selected for the exported
source. Licensing remains for the owner to decide. Dependencies retain their respective
licenses.

## Following a request through the pipeline

Private correlated events are in `<run-dir>/diagnostics/pipeline.jsonl`; the legacy
`voice.jsonl` sink is bounded too. Each retains up to four MiB plus one rotated
backup. Logging is best effort and never authorizes delivery. Events include text
and agent evidence, so treat the directory as private. Credentials and audio
payloads are excluded; oversized events retain correlation IDs and a bounded
preview, so some content can be truncated.

Find `request_id` in `turn_post`, `http_turn_input`, `provider_input`, tool and
`canonical_receipt` events. The receipt supplies `turn_id`. For voice, the browser
`transcript_dispatch` and decision events join `client_id`, `transcript_id` and
revision; `capture_id` joins raw recognition and audio events. Playback uses its
record ID plus generation/epoch. These IDs distinguish a capture, utterance,
controller turn and played response; they are not interchangeable.

| Layer | Emitted evidence | Limit |
| --- | --- | --- |
| Capture and wake | Activation/status, capture ID, recognition socket epoch | No raw microphone recording or physical acoustic measurement |
| Recognition and accepted text | Provider and browser transcript chunks, dispatch text/transcript revision | Provider may recognize words incorrectly; wake boundary still gates the first request |
| Semantic completion | Pause Jev input/result, decision input/result | Model judgment, not proof of intended wording |
| Controller | Haiku context/tool specifications, tool input/result, raw result and canonical receipt | Bounded context and diagnostics can omit older/oversized evidence |
| Delivery judgment | Jev initial/refined questions and results, final authorization/span | Historical logs predating these events cannot reconstruct those judgments |
| Recipient and paste | Captured identity, immutable draft and action receipt | Submitted input is not agent task completion; uncertain input never retries |
| Current evidence | Read intent, live/newest-turn snapshot, historical original | A snapshot describes its capture time, not later execution |
| Rendition | Exact original, preparation input/result and streamed TTS text | Synthetic preparation tests do not establish physical pronunciation |
| Audio | TTS chunk sizes/alignment, queued/ended/progress events and playback ledger | Queued or generated audio is not heard; unsupported alignment cannot prove a prefix |

For a wrong message, compare dispatch text with the Jev span and draft before
looking at the paste receipt. For wrong status, compare the newest-turn snapshot
with the historical original and final answer. For odd speech, compare the exact
original, rendition and TTS text before alignment and played progress. The full
stage joins were exercised with real HTTP/WebSocket/browser code, synthetic
inference/audio and disposable terminal conversations; existing live chats are
excluded from automated tests.

### Reconstructing one interaction

Use the read-only CLI to list recent anchored requests, then select an exact ID or
an unambiguous prefix. It reads the current pipeline log and its `.1` rotation.

```sh
python interaction_report.py --log /path/to/run/diagnostics/pipeline.jsonl
python interaction_report.py --log /path/to/run/diagnostics/pipeline.jsonl --request REQUEST_ID
python interaction_report.py --log /path/to/run/diagnostics/pipeline.jsonl --request REQUEST_ID --json --output /private/reports/request.json
```

Add `--session-id SESSION_ID` when an ID was reused across sessions. The readable
report highlights accepted wording, decisions, delivery and controller receipts,
originals, rendition and recorded playback acknowledgments, then gives a timeline
with file/line references. JSON includes the full selected events. Exports create
new files with mode 600 and new report directories with mode 700; existing files
are never overwritten. Standard output is an explicit display of private evidence.
Reports contain user text and agent evidence; keep them private.

Correlation uses known event envelopes, not IDs embedded in model context, history
or message text. HTTP input binds the transcript revision; a revisionless browser
post does not authorize later revisions. Playback IDs bind through application
ledger events. Audio generation is joined only for audio stages, so the next
utterance's dispatch generation cannot become the previous reply's playback.
A shared microphone capture does not identify an utterance: raw recognition is
separate contextual evidence, never joined by matching words or time. Missing
rendition or TTS IDs remain visible as unassociated sources. Ambiguous links are
excluded rather than guessed. An empty log or deployment probe lists zero interactions.

The reader bounds input to sixteen MiB and twenty thousand records across both
files, with a 128,000-byte line limit. Missing files, malformed/incomplete lines,
invalid timestamps, truncated events and limit omissions appear as gaps with
source references. Rotation can lose earlier stages, and diagnostics remain best
effort. A controller's `completed` status says its turn ended; `submitted` says
input was handed over. A full playback flag requires the recorded application
boundary acknowledgment; it does not independently establish physical hearing or
agent completion. The checked-in 99-event fixture is an actual synthetic caller
trace with two requests sharing one capture, not a live user recording.

## Visual workspace

Each selected tab has a **Creations** view and a passive **Live browser** view.
Creations are explicitly captured files, with immutable versions; changing or
removing the original does not change a saved version. A tab with several panes
shares its creations, with the source conversation shown. Its live browser appears
only for the selected matching pane and exact conversation. Selecting an output
version does not change Jarvis's subject or terminal focus; explicitly switching
the session or pane establishes the next message's subject.

Use the helper from this checkout and virtual environment. Copy the tab and pane
IDs from the Desk state (`GET /api/state`). The helper captures the current identity
once; supply `--identity` when retaining a previously captured identity is required.
An unresolved or replaced agent conversation blocks capture and browser attachment.

```sh
python visual.py --url http://127.0.0.1:54617 register \
  --tab @1 --pane %1 --title 'Prototype' /path/to/export/index.html

# Append a new immutable revision using the creation_id returned above:
python visual.py --url http://127.0.0.1:54617 register \
  --tab @1 --pane %1 --title 'Prototype' --creation CREATION_UUID /path/to/export/index.html

# Capture a static HTML bundle with relative JS/CSS/image assets:
python visual.py --url http://127.0.0.1:54617 register \
  --tab @1 --pane %1 --title 'Prototype bundle' --entrypoint index.html /path/to/export

python visual.py --url http://127.0.0.1:54617 list --tab @1
```

HTML, Markdown, plain text/CSV, PDF and common raster images are supported. Use
`--mode imported` for surviving older files: this records an import of today's
bytes, not a reconstructed historical version. The default `captured` records an
explicit current snapshot, without claiming the agent authored it. Save versions
explicitly; there is no automatic transcript backfill or filesystem watcher.
Downloads preserve original bytes, or a ZIP containing original bundle files.

Capture allows up to 128 files, 8 MiB per file and 16 MiB per version. Use a small
export directory: symlinks, hidden directories, credential-like names and runtime
files are rejected. HTML runs in an opaque sandbox, with scripts/CSS/images/fonts
limited to its captured bundle and inline resources; remote resources, network
requests, external iframe navigation, forms, embedded frames and privileged Desk
access are blocked. Open active previews through Desk; direct document navigation
to their capability URLs is rejected. This is
for self-contained static pages, not a development server or a general app runtime.
PDF display depends on browser support; the original download remains available.

To view an agent's browser, explicitly attach an already running **named** local
agent-browser session. Obtain its stream port and daemon PID from that session's
`.stream` and `.pid` records (under its configured `AGENT_BROWSER_SOCKET_DIR`).
Use the actual session the agent controls; opening a URL in a separate browser does
not attach the same state. Default or unidentified sessions are rejected.

```sh
python visual.py --url http://127.0.0.1:54617 attach \
  --tab @1 --pane %1 --name 'Prototype browser' \
  --session MY_NAMED_SESSION --port STREAM_PORT --pid DAEMON_PID

python visual.py --url http://127.0.0.1:54617 detach \
  --tab @1 --attachment ATTACHMENT_UUID
```

The relay only receives viewport images; it sends no browser input, navigation,
resize or other application commands. It follows that agent-browser session's
active page. Closing the view or detaching leaves the browser running. Replacement
of the pane/conversation, tmux server, daemon or stream listener invalidates the
attachment, which must then be attached explicitly again. A disconnected unchanged
session can be viewed again. For the legacy unmanaged agent-browser relay, opening a new viewer replaces its previous observer; switching
away closes only that observer, leaving the registration ready.
This integration targets native Linux agent-browser 0.33.0; other browser providers
and older Node-based agent-browser daemons are not supported by this first slice.

Saved bytes and provenance live in owner-only `<run-dir>/visual.sqlite3`. Restarting
Desk with the same run directory preserves versions and tab ownership while that
tmux server/window still exists. A new tmux server does not inherit history from a
reused socket path or window number. Closed-tab outputs retain their bytes and can be opened in the read-only archive.
Reassociation and automatic migration to recycled tabs remain excluded. Legacy
unmanaged agent-browser attachments are not restored on Desk restart; managed
browser ownership is retained by the private daemon and revalidated on observation. The visual API uses the
same loopback/private-proxy access boundary as Desk; it is not a multi-user service.

## Managed browser, outputs and app sessions

After starting Desk against a disposable or chosen tmux session, provision a new
managed agent once. Ordinary requests inside that conversation then use the Desk
browser/output tools automatically:

```sh
python launch.py managed-session 'Research' --socket /path/to/tmux.sock \
  --source-session tabs --run-dir /path/to/desk-run \
  --provider claude --workspace /path/to/project
```

Use `--provider codex` for Codex. `--account-profile work-account` selects a
separate persistent account environment; the default is `desk`. Account choice
is setup, not a model argument. App previews always use separate app storage.
The command adds session-local MCP configuration and lifecycle identity tracking;
it does not edit global Claude/Codex settings or the older restricted test
helpers. Claude waits for MCP startup and receives the small tool schemas upfront.
Codex requires the Desk MCP server and authorizes this explicitly provisioned
server's tools. Existing provider subscription stores stay in place.

`--resume EXACT_CONVERSATION_UUID` uses native provider resume after the original
process has stopped. It refuses a still-running conversation in the same tmux
server. The resumed conversation receives a new live binding and Desk tab; native
conversation history and the selected account's sign-ins continue. Previous page
handles are not reassigned across tabs. Saved outputs stay in the original
read-only archive. Uninstrumented browser tools cannot be adopted by URL, title
or whichever page is active.

Running Codex conversations can also be explicitly registered with
`live_connection.register` and the optional `desk_mcp.py --live` server. Each call
uses native MCP thread metadata and pinned pane, agent and server process
generations; unknown or replaced conversations cannot use another connection.
This leaves existing model, permission, plugin and credential settings in place.
In Codex 0.160.1, `/mcp` can display a newly configured server before its tools are
available to the agent. A verified idle, empty conversation can switch with
`/new` then `/resume EXACT_UUID` to refresh tools while retaining native history;
do not perform that switch during a turn or over an unsent draft. Verify an actual
native tool call after discovery before reporting the connection ready.

Managed tools open exact owned browser pages, create/publish completed files,
finish browser downloads, and start/stop app previews. Publication returns an
immutable receipt; identical receipt-key retries return the same version, while
conflicting reuse fails. Markdown captures local image references along with the
original text. Other supported files get exact download cards. Current capture
bounds remain 8 MiB/file, 16 MiB/version and 128 files. A pending download is not
saved until completion and a successful receipt.

App commands are argument arrays with a `{port}` placeholder and must bind to
`127.0.0.1`. Desk binds the isolated app browser to the exact process generation
and listener socket. Losing that listener makes the app's pages stale and blocks
their requests, controls and frames. Two app processes and two browser profiles are
the current limits; capacity errors do not evict active work. App processes receive
only basic locale/PATH variables plus an isolated HOME, PORT and HOST. Readiness
and bounded error output appear in Desk. Source identity is a supplied exact
build identifier or a bounded hash of non-hidden project files and command,
excluding dependency/cache directories. Initial and final screenshots are labeled
saved outputs with source/process/build provenance; stopping an app preserves
them. Screenshot or browser cleanup failure is reported while the verified app
process still stops.

Managed browser requests carry a runtime-enforced marker that Desk rejects,
including direct navigation and embedded frames through aliases or a proxy.
Managed profiles disable service workers so requests cannot bypass interception.

Visual selection is local to each device. A selected saved version can travel as
structured reference context for the actual message recipient through the managed
`desk_context` tool; the user's terminal message remains byte-for-byte unchanged.
The saved output's author never becomes delivery authority. Explicit selection
receipts include `view_target`, including recovered Jarvis receipts.
