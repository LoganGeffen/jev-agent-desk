# Verification

This file separates three kinds of evidence and never lets one stand in for another:

- **Measured**: a real provider (Jev, Claude Haiku 4.5, ElevenLabs) or a real
  loopback server and Chromium page was exercised, and the result was recorded.
- **Synthetic**: a fixture, fake provider, disposable tmux session or intercepted
  browser endpoint was exercised. This proves control flow and invariants, not
  model accuracy.
- **Unverified**: nothing was exercised. This includes every physical-device
  property.

Evidence is labelled by build stage (M1 to M5) and by the tree it ran on. Each stage
was verified on its own tree; no single full-suite run covers this branch's tree.
The product is **not accepted**: the final independent review of a combined,
immutable candidate and its combined full suite are later steps that are not
recorded here.

Run artifacts live in a private, uncommitted run directory
(`.run/jarvis-build-20261003` of the original checkout). They are referenced by name
below and are not part of this repository. They contain no credential values;
credential sources are recorded by path and configured variable names only.

## Not accepted yet

These are open. None is described as passing anywhere in this repository.

1. **Contextual send is not proven end to end against real providers.** The
   unchanged natural request "Ask it to explain those numbers with an example" was
   denied by Jev 1.13.0 before the authorization repair (permission deny, message
   start 0 instead of 10; `M5-PROVIDER-FINDINGS.json`, `SOL-M5-PROVIDERS.json`).
   After the repair, the focused authorization question allowed it with the exact
   span (start 10, end 47; `SOL-M5-AUTH-RECHECK.json`). The named read now works
   (`SOL-M5-NAMED-READ.json`: Haiku listed sessions and read Dog Data, and the
   subject became Dog Data independent of the visible Notes pane). The natural send
   and the explicit literal send were still blocked in that run: Jev allowed them
   with the correct target and the exact source span, but **every pane choice was
   "none"**, so nothing was delivered. This is real pane judgment, not only the
   earlier fixture numbering. A pane-question repair (concrete default pane id in
   the question; `SOL-M5-PANE.json`, commit `97c5c75`, 28 tools tests passing) was
   verified only with synthetic assertions and is **not in this branch's tree**.
   The real-provider recheck of the three blocked positive cases and the missing-pane
   negative is pending and owned by the provider run. Until it passes, no
   real-provider contextual or literal send is claimed.
2. **Speech compatibility is partial.** One synthetic phrase round-tripped
   through the real relay, but recognition changed capitalization, so the exact
   transcription check is false (see Measured: speech).
3. **Final independent review and combined full suite.** See Tests. This branch's
   tree has no recorded full `python check.py` run, and no independent review of the
   combined candidate is recorded.

### Resolved since the earlier version of this file

Each item below was a recorded open finding. The fix, the original reproduction
rechecked by someone other than the author, and, where relevant, a real-provider
case are listed separately.

- **M3 F1, cross-client audio revocation (`OPUS-M3.json`).** Client A kept playing
  after client B took over. Fix: commit `71cd555` in this branch (run
  `SONNET-M3-REVOKE.json`, tested commit `abcd6dc`, tree `975a9e5e`). Thirteen
  playback-flow tests passed, four new; the two revocation tests fail on the base
  `jarvis.js`/`voice.js` and pass with the fix. The **unchanged original Opus
  two-client probe was rerun** against that tree and, independently, by a non-author
  on the integrated tree (`M3-GATE.json`, `m3-fixed-original-probes.log`): A's player
  and sources were stopped and its socket closed, the stop button hid, zero new audio
  was scheduled after takeover, zero heard posts were made, and only B's full
  completion marked the reply heard. Controlled WebSocket and AudioContext; no
  physical audio.
- **M3 F2, filler after dispatch (`OPUS-M3.json`).** A later filler invalidated an
  already dispatched request. Fix: commit `73e8d59` in this branch (`SOL-M3-F2.json`,
  original commit `1e4421b`, tested tree `3b3c6fc2`). 27 turns and API tests passed.
  The unchanged original probe now shows the original request completed, exactly one
  terminal payload and the filler draft retained, and a non-author reran it
  (`M3-GATE.json`). Same-source invalidation, correction revocation and other-client
  isolation have their own negative tests.
- **F2 real-provider case (`SOL-M3-F2-PROVIDER.json`, tree `effae3a9`).** Three
  independent cases through the actual turn-decision path with the actual Jev
  (`jev-1.13.0`), one call each, 286, 279 and 353 ms. All three passed:
  "Actually, tell Controller to pause the queue." was complete with correction yes;
  "Actually, tell Controller to" was wait with correction yes; "Hmm." was wait with
  correction no. The two earlier real calls had returned correction no for both
  "Actually" utterances (`SOL-M5-PROVIDERS-initial.json`, preserved). The margin on
  the complete correction is modest (probability 0.74, confidence 0.48). Only the
  current decision used the real Jev; the controller, deliveries and terminal were
  not involved. Three utterances are not a general accuracy claim.
- **Jev `correction` flag.** Previously recorded as metadata only. It is no longer
  metadata: after the F2 fix a recognized correction or cancel revokes that client's
  earlier pending effects. Treat it as a model judgment with the measured margin
  above, not as proof.

## Restart announcement to the current client (M5)

Hypothesis: `recover()` cancels a running turn as interrupted with canonical
submitted or uncertain text, but the client only spoke failed receipts, so the page
that sent the request stayed silent after a server restart. Reproduced first with the
new test on unmodified `jarvis.js`: the two current-client cases timed out waiting
for any speech. Fix: commit `d9b381b` (tree `a941b9ef`), a narrow `jarvis.js`
change plus `test_jarvis_restart_browser.py`.

`test_jarvis_restart_browser.py` (6 tests) drives an actual Chromium page at a
loopback `server.make_server`, kills that server's connections, reopens the real
SQLite store in a new `Playground` and serves it on the **same port**, with a
controlled synthetic provider and disposable tmux fixtures. `VoiceControls.speak` is
captured, so audibility is judged by what the client asked to speak. It exercised:

| Case | Outcome |
| --- | --- |
| current pending client, input already submitted | speaks the stored canonical "Input submitted to Dog Data. Its task is not complete; no automatic retry." once; terminal input count 1; the new provider is never called; page sent exactly one turn POST |
| current pending client, nothing sent yet | speaks the canonical interruption text once; zero terminal input; no action recorded |
| page reloaded before the restart | quiet; no pending request; receipts still shown as superseded or cancelled |
| request superseded by direct mode during the outage | quiet; no redispatch |
| Stop & listen during the outage | quiet; agent unchanged; no redispatch |
| explicit cancellation of the current request, no restart | quiet |

Deliberately breaking the check that the request is still current makes both
non-current cases fail. The interrupted-flag restriction is a narrowing safeguard:
through the real server an ordinary cancellation never carries result text, so the
test cannot separate it. Nothing was measured on a physical device. The uncertain
case (a crash between input and receipt) is covered at the store and HTTP level by
the M4 evidence below, not repeated here in the browser.

## Measured

### Real Haiku through the restricted bridge (M1)

`haiku-probe-fixes.json`: model `claude-haiku-4-5-20251001`, subscription route.
The init tool list was exactly one bridge tool, the nonce came only from that
tool, there was one callback, first text at 1410 ms, 2637 ms for the call. The
natural five-turn scenario with actual Haiku and synthetic application tools passed
(`core-scenario-live.json`, repeated independently on the unchanged scenario at
`cb1f27a` in `opus-m1-recheck/live-scenario-cb1f27a.json`, 16.11 s total).
Provider time per turn was about 2.7 to 4.1 s; first text 1.4 to 1.9 s, which may be
a neutral sentence rather than the answer. End-to-end voice latency is **not
measured**.

### Real Jev and Haiku compatibility probe (M5)

`SOL-M5-PROVIDERS.json` (probe `42a0165`, base `64f020f`; 6 Jev HTTP calls, 5 Haiku
subscription calls; Jev actual model `jev-1.13.0`). Overall `passed: false`, status
`blocked_on_actual_contextual_send`.

| Check | Result |
| --- | --- |
| explicit literal send, exact span | passed |
| discussion authorizes no send | passed |
| complete vs unfinished pause decision | passed |
| unfinished pause waits | passed |
| harmless narrative (`narrative_check_ms` 302) | passed |
| contextual send | **failed** (deny, confidence 0.39) |
| correction flag | **failed** at this stage (both "Actually" utterances returned correction no); superseded by the F2 real-provider pass in Resolved above |

Jev calls took 271 to 329 ms; Haiku turns 2.8 to 8.8 s. The harness first ran with a
null handoff that broke the Jev question before the HTTP send cases; the handoff
was corrected, only the send cases were repeated, and the failures are preserved
in the artifact.

`narrative_check_ms` is a model-based safety judgment about whether an
output claims an effect. It is not deterministic proof that the text is true.
Acknowledgments of actions stay deterministic and are derived from receipts.

### Authorization repair recheck (M5)

`SONNET-M5-AUTH.json` describes the repair, commit `ae4f1ad` (present in this
branch's history as `8ff41d4`): the legacy filtered Jev question is replaced by a
compact authorization question owned by Jarvis, shrinking the Dog Data fixture
payload from 16,534 to 8,785 JSON characters, with 26 added tools tests (141 focused
tests).

`SOL-M5-AUTH-RECHECK.json` (6 Jev calls 290 to 579 ms, 2 Haiku calls 3.5 and 4.0 s):

| Case | Result |
| --- | --- |
| permission for the contextual request | allow (0.71), start 10, end 47, target = subject |
| explicit exact literal, recipient correction | Jev allowed; delivery blocked by fixture pane numbering |
| discussion, negation, injected evidence | denied, no delivery |
| Haiku plus Jev contextual send, end to end | **failed** (item 1 above) |

The three guard cases (discussion, negation, injected evidence) held; the
send-capable cases show only that the Jev judgment improved, not that delivery works.
The recheck ran on its own recorded tree, not on this branch's tree.

### Speech compatibility (M5)

`SOL-M5-SPEECH.json`: one text-to-speech and one speech-to-text through the actual
`voice.py` relay with a fake browser transport. Requested models were
`eleven_flash_v2_5` and `scribe_v2_realtime`; the relay does not echo model IDs. First
audio arrived at 456 ms (522 ms total); alignment was present on the first chunk.
The committed transcript arrived at 2706 ms (2760 ms total). The text "Read Dog
Data." was recognized as "Read dog data.", so `exact_transcription` is false and the
probe's `passed` is false even though both transports completed. No audio was
retained; there was no microphone, speaker or phone. The synthetic source text and the
recognized transcript are different strings and are reported separately.

Chunk alignment for partial heard prefixes was measured earlier on two short
synthetic streams (`SONNET-M3-PREFIX.json`); non-ASCII provider audio is unproven.

## Synthetic and controlled-browser evidence

Gate results below were recorded by the stage that produced them.

| Stage | What was exercised | Result |
| --- | --- | --- |
| M1 core, bridge | controller, restricted Haiku bridge, exact literal boundaries, single delivery attempt per turn | `M1-GATE.json` passed at `aaf5a72`; 226 Python, 48 Node, intercepted Chromium |
| M2 backend, browser | validated tools, receipt-derived speech, original replies, direct-mode switch, actual server and 390x844 Chromium to disposable tmux | `M2-GATE.json` passed at `8308edb`; 244 Python, 81 Node, Chromium |
| M3 voice, turns, playback | semantic pause, decision fencing, owner/epoch playback ledger, Unicode partial prefix, actual-page probes | combined run at `0565a48`: 262 Python, 149 Node, Chromium. Independent review `OPUS-M3.json`: **not accepted** at that commit (F1 and F2, both since fixed and rechecked, see Resolved above; F3 was a fixture race, fixed in `0565a48`) |
| M4 durability | SQLite ledger, restart recovery, jobs, continuity, byte bounds | `SOL-M4.json`; see Tests |
| M5 F1/F2 fixes | cross-client audio revocation; transcript-bound authorization and filler handling | `SONNET-M3-REVOKE.json`, `SOL-M3-F2.json`, nonauthor original-probe rerun in `M3-GATE.json`; see Resolved above |
| M5 restart announcement | actual Chromium, same-port HTTP restart, real SQLite, disposable tmux, synthetic provider | `SONNET-M5.json` run 8b755dea, commit `d9b381b`; 6 tests, see above |

Specific M4 behaviors exercised with real subprocess exits, the real SQLite store,
loopback HTTP and disposable tmux (`SOL-M4.json`): exit after the write-ahead record
and before the effect, and after external input and before the receipt, both recover
as uncertain with zero replay; a lost submitted response is answered from the
store on an identical repeated POST with one terminal input and no second provider
call; checkpoint failure prevents the external call; jobs restore as interrupted
without workers; exact partial playback restores without inferring heard.

Everything in this section used synthetic providers, intercepted browser
endpoints, or fake transports. It establishes control flow, not model quality.

## Tests

Counts are what each stage recorded for its own tree. Nothing here repeats them.

- M4 full `python check.py`: 302 Python, 149 Node, Chromium (`sol-m4-full-check.log`)
  on tree `814885a2`. The M4 final tree `52f8d1f` was verified only by 45 focused
  recovery/state/controller/store tests and 56 with the tools tests.
- F1 fix (`71cd555`, tested tree `975a9e5e`): 13 playback-flow tests; F2 fix
  (`73e8d59`, tested tree `3b3c6fc2`): 27 turns and API tests.
- Restart announcement (`d9b381b`, tree `a941b9ef`): the 6 new browser tests pass;
  neighbouring suites also passed, 150 Node tests and 19 Python tests. The new tests were checked for
  sensitivity: they fail on the unmodified `jarvis.js`, and deleting the
  "request is still current" condition fails both non-current cases.
- The last full tested tree recorded in the run state is `394c0fa2`. **No full
  `python check.py` run is recorded for this branch's tree.** The integrated suite
  and final independent review are pending, run by the lead, and not reported here.
- This documentation commit changes no code. It was checked only with
  `git diff --check`. Run the suite with the dependencies in `requirements-dev.txt`.

## Isolated checkout smoke

The README's isolated-run commands were run against a disposable tmux server with
no credentials by the earlier documentation run (`cc077555`, at `5ba0f91`). Evidence
is in `SONNET-M5.json`: loopback bind, an explicit port and run directory, the
Jarvis state endpoint, creation of the SQLite file, and clean shutdown. That run's
launcher receipt was invalid because of a future `not_run` check; this file
preserves the smoke evidence and that receipt does not count as acceptance. The smoke
was **not rerun** after later commits. It is a startup smoke test, not feature
verification, and it used no existing conversation.

## Unverified

- Physical phone microphone permission, acoustics, speaker output, mobile
  networking, and background or screen-lock behavior.
- End-to-end latency from speech to spoken answer. Component timings above are
  separate and are not summed.
- Non-ASCII provider audio alignment, long real conversations, and accuracy of the
  continuity classifier on real history.
- Behavior with existing real Codex and Claude conversations, which were not used.
- Any service restart or cutover. None was performed. The restart test restarts a
  loopback test server only.
- Whether a real Jev or Haiku conversation produces a deliverable pane choice (item 1
  of Not accepted yet).

## Artifacts

All names are in the private run directory unless noted.

- Provider: `SOL-M5-PROVIDERS.json`, `SOL-M5-AUTH-RECHECK.json`,
  `SOL-M5-SPEECH.json`, `M5-PROVIDER-FINDINGS.json`, `SONNET-M5-AUTH.json`
- Stages: `M1-GATE.json`, `M2-GATE.json`, `OPUS-M3.json`,
  `M3-REVIEW-INTEGRATION-NOTE.json`, `SOL-M4.json`
- Live Haiku: `haiku-probe-fixes.json`, `core-scenario-live.json`
- Fixes: `SONNET-M3-REVOKE.json`, `SOL-M3-F2.json`, `SOL-M3-F2-PROVIDER.json`,
  `M3-GATE.json`, `SOL-M5-NAMED-READ.json`, `SOL-M5-PANE.json`
- Restart announcement and this refresh: `SONNET-M5-FINAL.json`; earlier
  documentation run: `SONNET-M5.json`
- Repository: `README.md`, `ARCHITECTURE.md`, `test_jarvis_restart_browser.py`
