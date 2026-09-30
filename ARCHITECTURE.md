# Architecture

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

`reply_watch.py` associates new completed replies with their original request and
recipient. `session_questions.py` captures history plus live terminal evidence and
asks an isolated Codex app-server for an observer answer. `model_stream.py` reuses
up to two connections, creating a new ephemeral, read-only thread per request with
shell, web search, apps and plugins disabled. It streams answer deltas; it never
starts an observer turn in the real agent's conversation.
Configured MCP servers are explicitly disabled for each observer thread.

`voice.py` relays recognition and synthesis through ElevenLabs, keeping API keys
server-side. `microphone.js` converts captured audio to 16 kHz PCM. `voice.js`
manages drafts per pane, recognition pauses, readback cancellation and audio caches.
An identity change stops automatic sending; pressing Send explicitly accepts the
reviewed draft for the current conversation in that same pane. Backend validation
still rejects any identity change after that click. Delivery pastes and presses
Enter in one operation; uncertain delivery is never automatically retried.
`spoken_reply.py` first asks Jev whether original wording or a faithful rendition
is appropriate; an explicit mode bypasses that judgment. A rendition is generated
incrementally over NDJSON. A small in-memory cache shares background preparation
and readback of the same exact reply and mode. `readback.js` sends completed
sentences to the speech WebSocket and schedules incoming PCM audio immediately.
Observer answers already use spoken prose and bypass the rendition step.

The browser can recover a lost POST response through request IDs in polling.
Submitted means input delivery, not task completion. Uncertain outcomes never
automatically retry. Stop & listen cancels browser speech preparation/playback;
interrupting an agent is a separate action. Terminal mode is literal input and
has its own explicit pane identity checks.

The HTTP and WebSocket listeners bind to loopback. HTTP checks Host/Origin;
WebSocket checks Origin. Remote access requires a separately authenticated private
proxy. There are no accounts, multi-user isolation, public hosting, or automatic
crash recovery in this application.
