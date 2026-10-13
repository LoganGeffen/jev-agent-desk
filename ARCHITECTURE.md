# Architecture

There are two request paths. **Jarvis** is the browser default: a continuing
conversation owned by the app. **Direct dictation** is the selectable alternative
and the original path, described under [Direct path](#direct-path). The tmux, identity,
history and voice modules below serve both.

## Jarvis path

```text
browser (index.html, jarvis.js, voice.js)
  | POST /api/jarvis/turn-decision   semantic pause, voice only
  | POST /api/jarvis/turn            exact text, request_id, captured target
  v
server.py: loopback + Origin + session_id checks, unknown fields rejected
  -> jarvis_turns.JarvisTurns        decision/transcript fencing, playback ledger
  -> jarvis_controller.JarvisController.submit
       state guard -> provider run_turn -> tool dispatch -> response policy
       -> jarvis_haiku.run_turn      fresh Claude CLI call per turn
          <-> jarvis_mcp.ToolBridge  private loopback MCP, 8 calls, 60 s
       -> jarvis_tools.JarvisTools.execute / authorize
          -> session_actions -> provider adapter -> tmux delivery
  <- receipt-derived result {text, status, actions, readback}
jarvis_state.JarvisState  <-> jarvis_store.JarvisStore (SQLite)
```

**Modules.**

- `jarvis_state.py`: the application-owned record. Turns, requests (idempotent by
  `request_id`), drafts, actions, jobs, playbacks, clarifications, evidence
  excerpts, notices, decisions, continuity and the attention queue. It builds the
  bounded model context and runs startup recovery. It is the only place delivery
  status changes.
- `jarvis_store.py`: durable form of that record. One SQLite table of
  `(kind, id, value)` rows in WAL mode with `synchronous=FULL`. A checkpoint writes
  only the records that changed, and a failure raises so the caller does not
  proceed.
- `jarvis_controller.py`: runs one turn. It serializes effects under a lock, calls
  the injected provider, enforces the call budget and deadline, and falls back to a
  deterministic response if the provider errors. The provider is injected, so tests
  use a synthetic one.
- `jarvis_haiku.py`: the real provider. Each turn is a fresh `claude -p` call to
  `claude-haiku-4-5-20251001` with `--tools ''`, a strict MCP config, an allowlist of
  only the bridge tool names, no settings sources, hooks disabled, no session
  persistence, low effort and thinking disabled. API-key and provider overrides
  are removed so the subscription route is used. It rejects a turn if the init tool
  list differs from the bridge set.
- `jarvis_mcp.py`: a stdio MCP bridge bound to this process with schema validation,
  a maximum of 8 calls, serialized execution and cancellation.
- `jarvis_tools.py`: the application tools, authorization and receipt-derived
  speech. Core tools (`read_user_source`, `set_subject`, `prepare_literal`,
  `send_draft`) come from the controller; `JarvisTools` adds `list_sessions`,
  `list_ready`, `resolve_subject`, `read_reply`, `observe_session`, `select_session`,
  `interrupt_agent`, `observer_job`, `job_status`, `confirm_draft`, `list_pending`,
  `review_pending` and `discard_pending`.
- `jarvis_turns.py`: HTTP-facing turn decisions and the playback ledger.

**Control flow and authority.** The current user text is the only authoritative
input. Replies, tool output and chat titles reach the controller model as untrusted
evidence in a separate part of its context. Arbitrary controller prose is never
delivered to an agent. The application may deliver a verified generated draft when
the user authorizes composition. A send works this way:

1. The app records source boundaries over the user's own message
   (`set_literal_boundaries`); the model may only choose an offered `boundary_id`
   through `prepare_literal`, producing an immutable draft that carries
   current-turn provenance. An explicitly classified composition uses the entire
   original request as source and an app-generated candidate that passes four
   focused fidelity judgments. Haiku cannot provide draft text.
2. `send_draft` runs under the effect lock. `authorize` first validates the target
   identity again, then compares the target and exact text with the semantic
   decision for this turn (below). A confirmed pending draft instead requires the
   offer prompt in the immediately preceding turn.
3. Delivery status is written as `attempting` before input is sent, then becomes
   `submitted`, `uncertain` or `failed`. Only one delivery attempt is allowed per
   original user turn; later attempts, with any draft, fail as duplicates.
4. The spoken text comes from the receipt (`JarvisTools.response`), not from model
   prose: submitted means handed over and not complete; uncertain means it will
   not be retried; failure says nothing was submitted. A harmless reply with no
   tool call passes a model-based narrative check (`narrative_check_ms`) or is
   replaced. That check is a safety judgment; action acknowledgments are
   deterministic.

**Jev judgments.** Each is a focused question with its own typed choices, not a
general prompt:

- `authorization_question` (built in `jarvis_tools.py`): permission, target tab,
  pane, message start, end and form, for send and interrupt. Shown only the current
  message, tab names, pane facts and the subject. The target pane must come from
  the choices offered, so a missing or ambiguous pane blocks.
- `turn_decision_question` (`jev.py`): complete, wait or cancel, plus a correction
  flag. The correction question sees the exact previous accepted user words and
  judges revision intent separately from grammatical completeness, so an
  unfinished replacement still counts as a correction and a filler does not. The
  flag is consumed by the semantic pause below. Measured results are in
  VERIFICATION.md.
- the narrative safety check, the confirm and discard permission checks, and a
  continuity classifier over batches of eight older turns (`server.py`).

A Jev failure is a hold or a block. It never becomes a send.

**Subject and selection.** An explicit tab or pane selection establishes this
device's next Jarvis subject. The client captures its exact identity at draft or
speech onset and sends it as `subject`; the server validates it before starting
the turn. Drafts stay in their original mode/pane slot when the user switches.
Changed identities hold delivery, including manual Send. Polling never replaces
the device's choice. Legacy `capture_target` remains a passive hint.

`JarvisState` stores the turn's initial subject for authorization and a separate
resolved subject for successful named resolve/read/select tools. Provider context
and tool defaults use that turn, while remembered global subject supports legacy
callers that omit `subject`. Explicit null means no subject, never a fallback.
A result updates only its originating device when its request and selection are
still current; another device's remembered subject cannot retarget a draft.

**Semantic pause (voice).** `voice.js` commits a transcript, waits `silenceMs`
(700 ms) in Jarvis mode, and posts it to `/api/jarvis/turn-decision`. A *complete*
judgment returns a `decision_id` tied to the transcript id, revision, text, subject
and client. `JarvisTurns.commit` rejects a voice turn without the current, complete
decision; `JarvisTurns.guard` re-checks that before every controller tool call.
*Wait* rechecks at 4 s and again 8 s later (`maxRechecks=2`). New words, a
revision, or a mode switch invalidate older decisions. A server or Jev failure
holds the text. Explicit Send passes `explicit_send=true`. Direct mode keeps the
earlier 3 s timer and never calls the decision endpoint.

Authorization of a dispatched voice request is bound to its captured transcript id
and revision, not to whichever draft was most recent. A consumed authorization
therefore survives an unrelated later filler or waiting draft. A revision or speech
onset on the same transcript, or a recognized correction or cancel from that
client, revokes that client's earlier pending effects at once and retires the
transcript so it cannot be recommitted; other clients are untouched. A *complete*
decision that is neither a correction nor a cancel never dispatches or cancels by
itself. A submitted receipt is immutable: a correction after delivery never
manufactures a terminal interrupt or a replay.

**Playback ledger.** `/api/jarvis/playback` creates and updates a record for each
spoken readback: generated, scheduled and played are distinct states, owned by
`client_id` and an epoch. A completed status needs the full boundary id; a partial
`heard_prefix` is a Unicode-codepoint count with a SHA-256 of the prefix and
provider alignment. A newer owner cancels the older record in the ledger. Only
full completion marks the reply heard (`heard_reply`), and `/api/replies/heard`
remains the legacy acknowledgment for non-Jarvis readback. Stop & listen cancels
audio without touching the agent, and results arriving after a cancel cannot
restart it. The server cancels a displaced client's ledger record but cannot stop
that client's audio itself. The client keeps a registry of its own tracked
playbacks (exact player, session, epoch) and, on seeing its own record cancelled or
failed in a polled snapshot or in a rejected update, stops that exact player: audio
sources, socket and scheduling, and only that player's interface state. It does not
bump the generation, stop newer local playback, call the heard endpoint, restart
audio or touch the agent. A rejected update triggers a state refresh so revocation
is confirmed from the server record rather than assumed.

**Jobs and notices.** `observer_job` queues a bounded explanation of an agent's
evidence through the existing session-question path (two workers). Completion
creates a quiet notice, consumed through `job_status`; persistence errors surface as
diagnostics. Jobs never send and cannot be resumed after a restart.

**Persistence and recovery.** `Playground` opens `<run-dir>/jarvis.sqlite3` and
loads `JarvisState` from it, restores the attention queue, and `recover()` runs
before serving. In-flight deliveries become `uncertain` and are never replayed;
unsent prepared drafts and open clarifications become `needs_review`; running turns
are cancelled as interrupted; jobs become interrupted; playbacks that were
generating or playing become cancelled. No heard state is inferred. A repeated
request id returns the stored outcome. Continuity beyond the recent window is a
bounded summary of explicit user decisions with exact sources kept; pending drafts
and actions are never summarized. Not covered: process supervision, a second writer
on the same run directory, a corrupt database, and delivery whose receipt was lost,
which is reported uncertain.

**Browser client.** `jarvis.js` (`JarvisClient`) tracks pending requests per client,
recovers a lost POST response by polling `/api/jarvis/state` (10 s grace, 90 s
deadline) and says the message "was not resent". The destination switch in
`index.html` defaults to Jarvis; Pane mode calls `supersedeForDirect()` and uses
the legacy request path. The + button carries the selected tab, pane and process
identity to `/api/tabs/create`; the server validates it and uses that pane's
current directory. A typed, standalone `codex` or `claude` in a shell pane goes
through `/api/panes/start-agent` with the same exact identity. Codex starts with
terminal-title identity tracking; Claude starts with per-session lifecycle hooks
while retaining its normal settings.

Across a server restart the page keeps polling; once the same-port server returns
with the same `session_id`, a pending request settles from the stored turn. A turn
that `recover()` cancelled as interrupted carries the canonical text (for example
"Input submitted to Dog Data. Its task is not complete; no automatic retry."). The
client speaks that text once, only if the request is still the client's latest and
its readback generation is unchanged. A page reloaded after the restart has no
pending request and stays quiet. A request superseded by direct mode, or stopped
with Stop & listen, stays quiet. An ordinary cancellation without the interrupted
flag is not spoken. The client never resends the POST, so the provider, the action
and the terminal input are not repeated; the turn stays shown as cancelled with its
receipts.

## Web Push notifications

`ReadbackPreparation` remains the single browser-independent three-second history
scan. After provider identity, history and native activity are revalidated, it
passes only the current completed final reply identity to `PushNotifications`.
The newest history turn must match a native completed turn; older replies cannot
trigger while a newer turn is underway. Push failures are caught outside attention
and readback preparation. First attachment, process replacement, server restart
and recovery from unavailable evidence silently prime the current reply.
Later turn identities create deterministic events in `<run-dir>/push.sqlite3`.
The baseline, event and per-device delivery rows make polling, restart and native
process resume non-replaying. Pending or sending rows found at startup become
uncertain rather than being retried.

`push_notifications.py` owns the owner-only VAPID key, subscriptions, exact-device
mutes, event delivery and open state. It sends a small visible payload—tab
name, event ID and exact conversation target—through `pywebpush`.
It never receives reply text. Endpoints must be HTTPS production browser-vendor
origins, redirects are disabled, requests have a ten-second timeout, and 404/410
responses remove revoked subscriptions. Subscription and private-key material is
not included in shared Desk or Jarvis state.

`agent_activity.py` supplies the same native evidence to pane badges and the
background watcher. Codex activity comes from bounded, incremental lifecycle
reads of its native rollout, discovered through read-only metadata. A fresh
app-server can reconstruct an unfinished stored turn as interrupted, so that
snapshot is never activity authority. Claude hooks are checked against the
latest transcript turn. Missing, stale or contradictory evidence is unknown;
process presence and unversioned tmux status flags are not lifecycle evidence.
Stopped/failed turns are distinct from reply-ready. Historical originals remain
readable independently, and no state asserts overall task completion.

`notifications.js` registers the root-scoped service worker and exposes explicit
per-device enable/test/disable controls. Tests target the selected agent pane.
Mute changes revalidate the current exact
agent pane and retain the stable provider/conversation identity across native
process resume. `service-worker.js` claims existing pages on activation and handles
push and notification clicks, with no fetch handler or offline shell cache.
A reply click carries the immutable event,
tab, pane and conversation identity through the root query string. After Desk's
initial state and session load, the client and server both require that exact
target before selection, then closes Visuals for that tab; a missing or replaced
target produces a visible stale message without fallback. Open state stays in the
push database and never updates the audio heard ledger.

## Direct path

The browser's typed and spoken drafts converge on `/api/request`. It sends exact
composed text, a request ID, and a captured tab/pane/process-conversation identity.
`server.py` snapshots available targets, calls Jev, validates the selected action
and identity, then dispatches through `session_actions.py` to a provider adapter.
Jev first distinguishes a direct message, a routed message, inspection of existing
output, a workspace command, or cancelled input. Direct messages use the selected pane and complete original
text without boundary extraction. Routed messages resolve the recipient and text;
inspection chooses readback or an observer answer and identifies the subject session;
unnamed references use the selected session, while named references get a separate
lookup with no selected-session fallback.
workspace commands choose an action without message extraction. Code restricts each
branch's available actions. A delivery judgment rejects unsupported outer action
sequences without treating commands inside a message as workspace commands. Close requests
retain clarification and explicit confirmation paths.

`jev.py` builds typed choice questions for action, recipient, pane and message
boundaries. Message delivery slices the selected span from the original request;
`message_delivery.py` only generates text when indirect-question grammar requires
conversion. Literal messages retain their wording. Jev probabilities are model
judgments, not measured guarantees.

`codex_actions.py` reads history through Codex app-server and sends via tmux.
It reads thread metadata first: legacy histories use `thread/read`, while paginated
histories use `thread/turns/list` with full items and ascending cursor traversal.
Page failures never return a partial history as a complete result.
`pane_identity.py` reads `/proc`, writer locks and, when needed, the local Codex
metadata database in read-only mode. For daemon-backed Codex terminals, Linux `ss`
identifies the connected daemon; its live writer locks and a unique exact session
name matching the terminal title resolve the conversation. Missing or duplicate
names remain unresolved. This path requires `ss` from iproute2.
Identity is checked again before delivery.
Unresolved identity blocks the action instead of guessing the recipient.

`claude_runtime.py` binds a pane process to Claude's session ID using SessionStart,
SessionEnd, UserPromptSubmit, Stop and StopFailure hooks. `command()` constructs
the disposable sandbox invocation. The hook entry point is:

```sh
/absolute/path/to/.venv/bin/python /absolute/path/to/claude_runtime.py hook \
  --socket /path/to/tmux.sock --run-dir /absolute/path/to/.run/attached
```

It receives the Claude hook JSON on stdin and validates pane ancestry. To integrate
normal Claude sessions, register that command for the five lifecycle events in
your own Claude settings and use the same run directory as the web process.
`claude_actions.py` reads only the transcript selected by the validated binding
under `~/.claude/projects`. Alternate Claude config roots are not supported.

`reply_watch.py` keeps delivery reply notices separate from readback preparation.
A single background watcher discovers every identified agent pane and reads its
validated history, preparing the latest completed reply regardless of how the
request arrived. It runs without browser polling or voice activation and never
starts playback. Incomplete/interrupted turns and commentary are not prepared.
The same validated history scan feeds `attention.py`. Its queue tracks the latest
completed reply per pane/process/conversation and separates heard replies from
replies awaiting user input. Jev classifies the latter on demand; code orders
input requests before unread replies by first observation. Newer turns supersede
old input requests. Failed identity/history reads cannot recommend stale output.
The attention routing branch can recommend, list, select or read, but cannot send
or interrupt. Browser follow-ups carry the exact recommendation reference and
server-side pane validation runs before switching/readback. Only successful audio
completion acknowledges that reply through `/api/replies/heard`; cancellation,
errors and late completion cannot acknowledge a newer reply. Reply notices stay
silent. The queue is shared by clients of the web process and is checkpointed in the
Jarvis store, so its entries and heard state are restored on restart.
`session_questions.py` captures history plus live terminal evidence and
asks an isolated model for an observer answer. `model_stream.py` limits generation
to two concurrent calls. Claude model names use `claude_stream.py`, defaulting to
Haiku 4.5 with thinking disabled through the existing Claude CLI subscription.
Each request starts a fresh temporary session; tools, MCP, hooks, skills and session
persistence are disabled, and API-key/provider overrides are removed. Evidence is
passed on stdin. Failed or incomplete results fail the request without a fallback.
Explicit non-Claude model names retain the Codex app-server path: up to two reused
connections, each request creating a new ephemeral read-only thread with shell,
web search, apps, plugins and configured MCP servers disabled. Both paths stream
answer deltas without starting a turn in the real observed agent conversation.

`voice.py` relays recognition and synthesis through ElevenLabs, keeping API keys
server-side. `microphone.js` converts captured audio to 16 kHz PCM. `voice.js`
manages drafts per pane, recognition pauses, readback cancellation and audio caches.
The per-device activation preference defaults to Tap only. Wake word and Always on
try microphone access on visible load and when returning to the foreground, in
either destination mode. Browser permission and running audio contexts are required;
failure leaves an explicit mic button. A manual mic stop suppresses restart until
the next page load or manual start. Switching destination restarts active capture
while preserving its activation setting.
`wake.js` uses the same mono 16 kHz PCM through a pinned local microWakeWord
pipeline, with all WASM/model assets under `/wake-assets/`. Idle detection opens
no recognition socket. The bounded three-second pre-roll plus request is sent
only after wake, and transcript authority begins only after a recognized wake
boundary. Playback readiness is separate from cloud-recognition readiness, so
closing STT on dormancy leaves response audio available. An awake conversation
reuses recognition for separate utterances; sixty seconds of inactivity returns
to local waiting. Pending work and playback defer inactivity. Each new utterance
binds fresh context, while a resumable timeout draft keeps its original context. Capture and wake-epoch
checks fence late initialization, socket callbacks and timeouts. Stop/hide/error
disposes the detector and both audio contexts. The official model threshold is
unchanged. This path has no backend wake classifier or browser SpeechRecognition.
An identity change stops automatic sending; pressing Send explicitly accepts the
reviewed draft for the current conversation in that same pane. Backend validation
still rejects any identity change after that click. Delivery pastes and presses
Enter in one operation; uncertain delivery is never automatically retried.
`spoken_reply.py` generates faithful renditions directly, without a Jev presentation
decision. Original mode returns exact text without generation. The legacy auto mode
and spoken mode share one cache entry. Renditions are generated incrementally over
NDJSON. The in-memory cache shares preparation and readback by exact reply text,
retaining each attached session's latest reply plus a small recent-history allowance.
`readback.js` sends phrases of up to six complete words, or through punctuation
followed by whitespace, whichever boundary comes first. Punctuation inside a
number or identifier is retained; unfinished words wait and the final tail flushes
at completion. It schedules incoming PCM audio immediately.
Observer answers already use spoken prose and bypass the rendition step.

The browser can recover a lost POST response through request IDs in polling.
Fullscreen Codex panes expose `remote_scroll` alongside their captured screen.
At a captured screen edge, touch and wheel scrolling use `/api/scroll` to send
PageUp or PageDown to the captured pane. The server revalidates its identity,
provider and alternate-screen mode before sending. Normal terminal scrollback
remains browser-local; fullscreen navigation changes the shared terminal viewport.
Submitted means input delivery, not task completion. Uncertain outcomes never
automatically retry. Stop & listen cancels browser speech preparation/playback;
interrupting an agent is a separate action. Terminal mode is literal input and
has its own explicit pane identity checks.

The HTTP and WebSocket listeners bind to loopback. HTTP checks Host/Origin;
WebSocket checks Origin. Remote access requires a separately authenticated private
proxy. There are no accounts, multi-user isolation, public hosting, or process
supervision in this application. The Jarvis ledger is durable across restarts, but a
crashed web process must be restarted by you.

## Correlated diagnostics and observation

`diagnostics.py` records best-effort events to `diagnostics/pipeline.jsonl` under
the run directory; `Voice` uses the same bounded sink for legacy `voice.jsonl`.
Each has a four-MiB limit and one backup. Credential-shaped fields and relevant
in-process secrets are scrubbed; audio payloads are never recorded. Correlation
IDs survive oversized previews. Logging exceptions, including argument preparation,
must remain outside authority and delivery control flow.

The join is capture/socket epoch → client/transcript revision → request/turn →
draft/action/recipient identity → rendition/playback generation and ledger. Browser
recognition and accepted dispatch text, focused pause decisions, Haiku context and
tool traffic, initial/refined Jev authorization, captured identity/delivery receipts,
current evidence, rendition text, TTS alignment and actual playback progress are
recorded separately. README's coverage table states the missing physical/provider
observations; no raw audio or blanket claim of end-to-end acoustic fidelity exists.

`read_reply` retains the latest completed original but also captures existing
`session_questions.session_snapshot` freshness. A focused read-intent judgment
separates literal reading, summary and present status. `observe_session` supplies
that bounded current snapshot directly so the same controller turn can answer;
`observer_job` continues to mean an explicitly queued quiet background observation.
The completion watcher's `latest_reply` semantics are unchanged. Summaries retain
checked model prose and expose originals separately; rejected/failed narrative
verification does not fall back to a historical original for present status.

Literal message preparation rejects spans crossing a paired quote wrapper.
The semantic form judgment separates exact, indirect and lightly composed forms.
Composition generation receives the full current request and validated recipient;
focused Jev judgments reject changed meaning, omitted details, invention and
unresolved references. A passing candidate is stored with its exact source in an
immutable current-turn draft. Failed generation or fidelity retains an unsendable
`compose` clarification; older such records cannot become sendable. Diagnostics
retain the form, source, candidate and judgment. Unmatched literal quote/code data
and full multi-sentence literals remain supported.

## Visual workspace

`visual_workspace.py` owns saved creations and passive browser observation, outside
Jarvis, delivery, terminal control and audio. `server.make_server` creates its store
and dispatches `/api/visual/*` plus version-scoped `/visual-preview/*` capabilities.
`visual.py` is the explicit registration helper; it snapshots regular files using
`openat`-style descriptor traversal with `O_NOFOLLOW`, then uploads bounded bytes.
The server never reads a supplied local path or proxies an arbitrary URL.

A workspace is keyed by Linux boot ID, tmux server PID/start time, socket device and
inode, and tmux window ID. Window IDs do not repeat within a server incarnation.
SQLite persists the random workspace ID, creations, immutable version records and
all copied bytes atomically. Exact pane identity and resolved provider conversation
are checked before capture and again before publication. A creation can gain a
version only in its existing workspace and kind; source provenance remains on each
version even after the agent changes. UI content/download requests include both
workspace and version; cross-workspace combinations fail. Archived bytes survive a
missing tab and remain available through the read-only archive API.

HTML capability responses set `Content-Security-Policy: sandbox allow-scripts`
without `allow-same-origin`, and the iframe repeats that sandbox. This makes the
content's effective origin opaque. Active capability documents are accepted only
as iframe/image requests; standalone browser document navigation is rejected.
Desk's parent `frame-src 'self' blob:` policy blocks a preview from navigating its
own frame to an external origin. The resource policy permits only this version's immutable assets plus
inline scripts/styles/data images; `connect-src`, `form-action`, `frame-src` and
`object-src` are denied. Capabilities expose no directory listing or filesystem.
Preview routes validate Host and capability; all privileged visual API routes
retain Desk's Host/Origin boundary and reject cross-site fetches. Binary downloads
always use attachment disposition and `nosniff`. Capability URLs stay private and
are never sent upstream to agent-browser.

Attachments bind a workspace and exact pane/conversation to one explicitly named
agent-browser daemon. Linux `/proc` checks the executable, selected daemon/session
environment entries, PID start time and owned loopback listener inode. Those
bindings are rechecked during observation and immediately before forwarding a
frame. Any mismatch latches stale and never reconnects to a replacement endpoint.
The SSE route opens a receive-only WebSocket; only validated JPEG/PNG frame packets
are forwarded, with local sequence numbers and an attachment generation. Provider
command/result traffic is discarded. There is no viewer-to-upstream channel.
Frames are bounded in size and the upstream receive queue is bounded; updates are
not dropped, so a static page’s final repaint remains observable.
A client disconnect closes only its observation socket; it never kills a browser.
For the legacy unmanaged agent-browser relay, a new observer supersedes the previous connection using a per-connection cancellation
event. Old callbacks and cleanup cannot reset the replacement observer or its state.
The frontend fences requests, stream callbacks and image decoding by selection and
generation. The legacy live connection is ephemeral, whereas creation history is durable. Managed browser streams support multiple independent observers and revalidate daemon-owned pages across Desk restart.

## Managed sessions and runtimes

`managed_session.py` provisions native Claude/Codex sessions with per-invocation
MCP settings; `desk_mcp.py` is the stdio adapter. A private session record is
created by the actual pane launcher before exec. `managed_provider.py` verifies
its capability and exact pane process generation, resolves native conversation
identity using the existing tracking, and persists the resulting owner binding.
Every operation revalidates that binding. Browser effects also use the daemon's
owner challenge before delayed execution. Models provide page handles within
that binding, never tab/process/account authority. The bridge limits file capture
to the provisioned workspace and traverses without following symlinks.

The publication table makes receipt keys idempotent within the immutable
workspace. Archive routes read original workspace IDs without reopening tmux
ownership. The reference inbox validates stored version identity, then binds
context to the actual validated recipient at delivery without changing the
literal message. Its context contains the original delivered message separately
from immutable source provenance.

`managed_apps.py` owns bounded app processes, cwd/command/build identity,
listener readiness, isolated browser profiles and saved screenshots. Runtime
state checks PID start time; a saved screenshot is a version, not an executable
historical app. Process environment filtering excludes Desk/provider credentials.
App stop validates ownership again after capture and reports capture/cleanup
failures separately from termination.

`app_runtime_identity.py` captures the exact process generation, cwd and owned
loopback listener inode. App profiles retain this binding; request interception,
delayed controls and completed frame capture revalidate it. Listener replacement
invalidates only that app profile, including its background requests. All managed
requests receive a forced marker that Desk rejects before dispatch, independent
of Host aliases. Service workers are blocked to preserve interception, and Desk
also denies framing its control UI.
