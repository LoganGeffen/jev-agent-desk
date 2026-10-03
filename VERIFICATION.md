# Verification

This file separates three kinds of evidence and never lets one stand in for another:

- **Measured**: a real provider (Jev, Claude Haiku 4.5, ElevenLabs) or a real
  loopback server and Chromium page was exercised, and the result was recorded.
- **Synthetic**: a fixture, fake provider, disposable tmux session or intercepted
  browser endpoint was exercised. This proves control flow and invariants, not
  model accuracy.
- **Unverified**: nothing was exercised. This includes every physical-device
  property.

Evidence is labelled by build stage (M1 to M5) and by the tree it ran on. The
combined M4 identity/continuity candidate `653c4dd` (tree `346a1bfb`) passed
341 Python tests, 153 Node tests and Chromium checks. Its unchanged original
identity and continuity probes passed in nonauthor rechecks; independent
`OPUS-FINAL-DELTA.json` closed those blockers (`M4-GATE.json`).
The later prepared-message repair is integrated at `8cd019c` (runtime tree
`058f3c3d`). Its exact-runtime natural Haiku/Jev composition passed; see Measured.
The suite on that backend runtime passed: 344 Python tests, 153 Node tests and
Chromium checks (`final-prepared-check.log`, exit 0). The final client `0430421`
(tree `0b7fc72d`) exposes incidental originals as read-only text. Its final release
suite passed 345 Python tests, 156 Node tests and both Chromium checks, exit 0
(`final-release-check.log`). Independent M4 delta review applies to `653c4dd`,
not to the later prepared-result or client changes.

Run artifacts live in a private, uncommitted run directory
(`.run/jarvis-build-20261003` of the original checkout). They are referenced by name
below and are not part of this repository. They contain no credential values;
credential sources are recorded by path and configured variable names only.

## Current verification limits

- **Speech compatibility is partial.** One synthetic phrase round-tripped through
  the real relay, but recognition changed capitalization, so the exact transcription
  check is false (see Measured: speech). Physical-device behavior remains unverified.
- **Evidence scope.** The backend `8cd019c` suite and two-turn real composition passed.
  Independent M4 delta closure applies to `653c4dd`; synthetic tests and bounded
  provider cases do not establish general model accuracy or physical-device behavior.

### Resolved since the earlier version of this file

Each item below was a recorded open finding. The fix, the original reproduction
rechecked by someone other than the author, and, where relevant, a real-provider
case are listed separately.

- **Prepared but unsent follow-up.** The exact-runtime `653c4dd` probe prepared
  the supplied body but did not send it, and canonical output replayed earlier
  readback (`SOL-FINAL-PROVIDER.json`, preserved). Repair `8cd019c` exposes the exact
  prepared text and source provenance to Haiku, clarifies draft usage, and offers
  explicit recovery when preparation ends without sending. That unsent status
  outranks earlier readback; the full original remains `available_readback`, while
  `readback` denotes the primary read response. No automatic send or retry was
  added. Fifty-nine controller/tools tests passed (`SOL-PREPARED-RESULT.json`).
  The unchanged natural two-turn real composition then passed at the repaired
  runtime (`SOL-FINAL-PROVIDER-REPAIRED.json`); its recorder limitation is below.
- **Incidental original in the client.** Final client `0430421` (tree `0b7fc72d`)
  preserves `available_readback` through immediate presentation and reload/polling,
  separately from the canonical response. It is exact read-only text, including
  newlines, Unicode and literal HTML; it has no playback control. Primary reads keep
  their existing controls, and automatic/manual playback of a canonical status uses
  that status's ledger source. `SOL-AVAILABLE-READBACK-CLIENT.json`: 59 Node tests
  and one actual HTTP/Chromium test with prepared-unsent and submitted subcases pass.
  Both retain the original after reload with no new audio; exact canonical manual
  playback is observed through the actual HTTP ledger. Root independently reviewed
  the runtime/browser diff. No backend or provider calls changed.
- **Pane resolution.** Repair `04ea6f0` makes the default pane identity explicit
  while preserving explicit number/role overrides. `SOL-M5-PANE-RECHECK.json`
  passed four cases on probe `315b1bf`, runtime tree `9eae6f9a`: the unchanged
  contextual follow-up, exact literal, recipient correction and missing-pane
  denial. This used real Haiku/Jev with synthetic workspace and delivery callbacks.
- **M4 captured identity (P2).** Fix `653c4dd` keeps the turn-captured subject
  through same-turn read, resolve and set-subject calls. A replacement holds the
  exact intended send body for explicit confirmation of an offered identity; a
  second replacement holds again. Read/resolve confirmation changes the subject
  without a draft or send authority. Eight regressions and all 36 tools tests pass
  (`SOL-M4-IDENTITY.json`); the lead reran the unchanged original P2 successfully.
- **M4 continuity (C1).** Fix `f6f4255` bounds observed reply text in model history
  without changing the stored original. The nonauthor unchanged P8 recheck
  (`P8-FIXED-RECHECK.json`) retains 23 recent turns, the newest user correction and
  prior preference after a 34 KB readback; model context is 20,866 bytes. A later
  20 KB readback gives 27,795 bytes. Exact originals remain available.
- **M3 F1, cross-client audio revocation (`OPUS-M3.json`).** Client A kept playing
  after client B took over. Fix: integrated commit `dbfc6d8` (source `71cd555`; run
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
  already dispatched request. Fix: integrated commit `9c47b27` (source `73e8d59`; `SOL-M3-F2.json`,
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
branch's history as `b2ed6e5`): the legacy filtered Jev question is replaced by a
compact authorization question owned by Jarvis, shrinking the Dog Data fixture
payload from 16,534 to 8,785 JSON characters, with 26 added tools tests (141 focused
tests).

`SOL-M5-AUTH-RECHECK.json` (6 Jev calls 290 to 579 ms, 2 Haiku calls 3.5 and 4.0 s):

| Case | Result |
| --- | --- |
| permission for the contextual request | allow (0.71), start 10, end 47, target = subject |
| explicit exact literal, recipient correction | Jev allowed; delivery blocked by fixture pane numbering |
| discussion, negation, injected evidence | denied, no delivery |
| Haiku plus Jev contextual send, end to end | **failed** in this earlier recheck |

The three guard cases (discussion, negation, injected evidence) held; the
send-capable cases show only that the Jev judgment improved, not that delivery works.
The recheck ran on its own recorded tree, not on this branch's tree.

### Pane repair recheck and exact-runtime composition (M5)

`SOL-M5-PANE-RECHECK.json` passed the four cases listed in Resolved: requested and
actual Haiku `claude-haiku-4-5-20251001`, requested `jev-latest`, actual
`jev-1.13.0`. Four Jev calls took 497, 357, 322 and 294 ms; two Haiku calls took
4565 and 5464 ms. The natural read/follow-up selected Dog Data while Notes stayed
visible and delivered exactly "explain those numbers with an example" once.
Injected reply instructions supplied no authority. An explicit missing pane sent
nothing and retained the exact source. Prior misses remain in
`SOL-M5-PROVIDERS-initial.json`, `SOL-M5-AUTH-RECHECK.json` and
`SOL-M5-NAMED-READ.json`.

The later `SOL-FINAL-PROVIDER.json` used the actual modules from `653c4dd`, tree
`346a1bfb`, including the archive tool and continuity context. Two real Haiku calls
and one real Jev call completed (Haiku 5904 and 7053 ms; Jev 415 ms).
"Read Dog Data" worked; the unchanged "Ask it to
explain those numbers with an example" called read and prepared a draft, then asked
for exact message text instead of sending. Zero delivery callbacks occurred. Notes
stayed visible, the subject stayed Dog Data, and reply evidence remained untrusted.
This failure is preserved unchanged.

`SOL-FINAL-PROVIDER-REPAIRED.json` exercised the exact clean runtime `8cd019c`,
tree `058f3c3d`, after the prepared-result/adapter repair. Actual Haiku
`claude-haiku-4-5-20251001` calls took 5294 and 6887 ms; actual Jev `jev-1.13.0`
took 304 ms. The unchanged read/follow-up called read, prepare and send. The
captured app action contains one submitted Dog Data payload exactly
"explain those numbers with an example", and one send tool receipt confirms it.
Canonical output reports submission and explicitly says the task is not complete.
The exact original reply remains available and evidence grants no authority.

The provider run passed with a recorder limitation: final report serialization
encountered a fixture callback that is not JSON serializable. Incremental actual
receipts and tool counts were preserved; no provider calls were repeated. The
post-run full state snapshot, raw delivery callback list and selected-pane
observation are unavailable and are not reconstructed. Both inputs captured Notes,
and the fixture observe function remained fixed to Notes with no focus tool call;
this is not a separate final selection measurement. The final client `0430421` leaves the provider/controller/tools/state source hashes
unchanged from measured `8cd019c` (`FINAL-HYGIENE-DELTA.json`); no new provider
call was made for the client or docs. This two-turn composition and the earlier four authority cases are bounded
evidence, not general routing accuracy.
All these runs used synthetic workspace/delivery callbacks without existing
terminals, microphone, speaker or phone.

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
| M5 actual mobile story | actual 390x844 Chromium to HTTP and disposable tmux: exact follow-up, slow observer plus Controller read, incomplete/corrected semantic request, quiet completion, Unicode aligned stop | `SOL-M5.json`; 3 tests pass, independently rerun with the 6 restart cases in `OPUS-M5.json` |
| M5 restart announcement | actual Chromium, same-port HTTP restart, real SQLite, disposable tmux, synthetic provider | `SONNET-M5.json` run 8b755dea, commit `d9b381b`; 6 tests, see above |

Specific M4 behaviors exercised with real subprocess exits, the real SQLite store,
loopback HTTP and disposable tmux (`SOL-M4.json`): exit after the write-ahead record
and before the effect, and after external input and before the receipt, both recover
as uncertain with zero replay; a lost submitted response is answered from the
store on an identical repeated POST with one terminal input and no second provider
call; checkpoint failure prevents the external call; jobs restore as interrupted
without workers; exact partial playback restores without inferring heard.

Remaining measured durability costs (`OPUS-M4.json`): synchronous whole-state
checkpoints took about 20 ms at 50 readback turns, 120 ms at 300 and 170 ms at 600
(1.6, 9.8 and 19.7 MB respectively). An injected 2 s checkpoint blocked a foreground
read for 2.1 s. Long-lived ledgers can degrade foreground latency; this remains a
limitation. An oversized 62 KiB user source is rejected with a generic message and
is not retained.

Everything in this section used synthetic providers, intercepted browser
endpoints, or fake transports. It establishes control flow, not model quality.

## Tests

Counts are what each stage recorded for its own tree. Nothing here repeats them.
Fixture runs emit unclosed SQLite connection ResourceWarnings during garbage
collection. This is a pre-existing test-harness limitation; these logs do not
establish a persistent-service leak or a verified cleanup mechanism.

- M4 full `python check.py`: 302 Python, 149 Node, Chromium (`sol-m4-full-check.log`)
  on tree `814885a2`. The M4 final tree `52f8d1f` was verified only by 45 focused
  recovery/state/controller/store tests and 56 with the tools tests.
- F1 fix (`71cd555`, tested tree `975a9e5e`): 13 playback-flow tests; F2 fix
  (`73e8d59`, tested tree `3b3c6fc2`): 27 turns and API tests.
- Restart announcement (`d9b381b`, tree `a941b9ef`): the 6 new browser tests pass;
  neighbouring suites also passed, 150 Node tests and 19 Python tests. The new tests were checked for
  sensitivity: they fail on the unmodified `jarvis.js`, and deleting the
  "request is still current" condition fails both non-current cases.
- Combined pre-fix candidate `6f0233f`, tree `a624058e`: 330 Python, 153 Node and
  Chromium conversation-browser checks passed (`final-candidate-check.log`).
  `OPUS-M5.json` independently reran the 3 actual mobile story and 6 restart browser
  tests, 9 passing in 42.5 s, and checked lost-response reconciliation and stopping
  readback without cancelling a running observer.
- M4 fixed runtime `653c4dd`, tree `346a1bfb`: 341 Python, 153 Node and Chromium
  passed (`final-fixed-check.log`). `OPUS-FINAL-DELTA.json` independently accepted
  identity/continuity closure, including unchanged P2/P8 and additional laundering,
  repeated-replacement and resolve-without-send challenges (`M4-GATE.json`).
- Latest prepared-result runtime `8cd019c`, tree `058f3c3d`: 344 Python tests
  (178.813 s), 153 Node tests and both Chromium checks passed, `check_exit=0`
  (`final-prepared-check.log`). The real composition passed separately; independent
  M4 delta review applies to the preceding `653c4dd` tree.
- Final text-only client `0430421`, tree `0b7fc72d`: 345 Python tests (191.113 s),
  156 Node tests and both Chromium checks passed, `check_exit=0`
  (`final-release-check.log`). Scoped client checks are recorded above.
- This documentation commit changes no code. It was checked only with
  `git diff --check` and local Markdown link checks. The recorded runtime code is
  unchanged by this documentation commit. Run the suite with the dependencies in `requirements-dev.txt`.

## Isolated checkout smoke

The README's isolated-run commands were run against a disposable tmux server with
no credentials by the earlier documentation run (`cc077555`, at `5ba0f91`). Evidence
is in `SONNET-M5.json`: loopback bind, an explicit port and run directory, the
Jarvis state endpoint, creation of the SQLite file, and clean shutdown. That run's
launcher receipt was invalid because of a future `not_run` check; this file
preserves the smoke evidence and that receipt does not count as acceptance. The smoke
was **not rerun** after later commits. It is a startup smoke test, not feature
verification, and it used no existing conversation.

`FINAL-HYGIENE-DELTA.json` verifies the final client checkout was clean, the four
measured backend source hashes match `8cd019c`, the original checkout's nine
pre-existing changed/untracked files retain their baseline hashes, dependencies
are unchanged, and no runtime/credential paths were committed. No service cutover
or existing conversation interaction occurred.

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
- General model routing accuracy beyond the measured cases. Initial failures and
  subsequent repaired passes remain recorded separately above.

## Artifacts

All names are in the private run directory unless noted.

- Provider: `SOL-M5-PROVIDERS.json`, `SOL-M5-AUTH-RECHECK.json`,
  `SOL-M5-SPEECH.json`, `M5-PROVIDER-FINDINGS.json`, `SONNET-M5-AUTH.json`,
  `SOL-M5-PANE-RECHECK.json`, `SOL-FINAL-PROVIDER.json`,
  `SOL-FINAL-PROVIDER-REPAIRED.json`
- Stages: `M1-GATE.json`, `M2-GATE.json`, `OPUS-M3.json`,
  `M3-REVIEW-INTEGRATION-NOTE.json`, `SOL-M4.json`
- Live Haiku: `haiku-probe-fixes.json`, `core-scenario-live.json`
- Fixes: `SONNET-M3-REVOKE.json`, `SOL-M3-F2.json`, `SOL-M3-F2-PROVIDER.json`,
  `M3-GATE.json`, `SOL-M5-NAMED-READ.json`, `SOL-M5-PANE.json`,
  `SOL-M4-IDENTITY.json`, `SOL-M4-CONTEXT-FIX.json`, `P8-FIXED-RECHECK.json`,
  `SOL-PREPARED-RESULT.json`
- Restart announcement and this refresh: `SONNET-M5-FINAL.json`; earlier
  documentation run: `SONNET-M5.json`
- Final story/review: `SOL-M5.json`, `OPUS-M5.json`, `OPUS-FINAL-DELTA.json`,
  `M4-GATE.json`, `final-candidate-check.log`, `final-fixed-check.log`,
  `final-prepared-check.log`, `final-release-check.log`,
  `SOL-AVAILABLE-READBACK-CLIENT.json`, `FINAL-HYGIENE-DELTA.json`
- Repository: `README.md`, `ARCHITECTURE.md`, `test_jarvis_browser.py`,
  `test_jarvis_restart_browser.py`
