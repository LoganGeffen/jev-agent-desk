# Verification

This file separates three kinds of evidence and never lets one stand in for another:

- **Measured**: a real provider (Jev, Claude Haiku 4.5, ElevenLabs) or a real
  loopback server and Chromium page was exercised, and the result was recorded.
- **Synthetic**: a fixture, fake provider, disposable tmux session or intercepted
  browser endpoint was exercised. This proves control flow and invariants, not
  model accuracy.
- **Unverified**: nothing was exercised. This includes every physical-device
  property.

Evidence is labelled by build stage (M1 to M5). Each stage was verified on its own
tree; no single full-suite run covers the final tree. A final independent review
that combines the stages on an immutable candidate is a later step that is not
recorded here.

Run artifacts live in a private, uncommitted run directory
(`.run/jarvis-build-20261003` of the original checkout). They are referenced by name
below and are not part of this repository. They contain no credential values;
credential sources are recorded by path and configured variable names only.

## Not accepted yet

These are open. None is described as passing anywhere in this repository.

1. **Contextual send is not proven end to end against real providers.** The
   unchanged natural request "Ask it to explain those numbers with an example"
   was denied by Jev 1.13.0 before the local repair (permission deny, message
   start 0 instead of 10; `M5-PROVIDER-FINDINGS.json`, `SOL-M5-PROVIDERS.json`).
   After the repair, the focused authorization question allowed it with the exact
   span (start 10, end 47; `SOL-M5-AUTH-RECHECK.json`). That recheck's own
   end-to-end case still failed: Haiku resolved "Read Dog Data" to the visible
   Notes pane, so the subject became Notes rather than Dog Data, and all three
   delivery cases (contextual, explicit literal, recipient correction) were
   blocked because the fixture's pane number was 0 while the server uses
   `len(panes)+1`, so every pane choice returned none. The fixture was corrected
   afterward in probe commit `fbe04e2` and **not rerun**. No delivery occurred.
2. **M3 F1, cross-client audio revocation (`OPUS-M3.json`).** When client B takes
   over a playback, client A's already-playing audio keeps playing; A learns of the
   revocation only from a rejected ledger post. No heard state is fabricated (only
   B's full completion acknowledges the reply), but two devices can speak at once.
   A fix is owned by another run and is not recorded as accepted.
3. **M3 F2, filler after dispatch (`OPUS-M3.json`).** A later turn-decision from
   the same client, such as a filler utterance, invalidates an already dispatched
   voice request before its effect. Nothing is misdelivered or duplicated and the
   screen shows "Superseded or cancelled", but nothing is spoken and the request
   is lost. The intended rule is awaiting adjudication.
4. **Jev `correction` metadata.** In the measured calls the correction flag was
   false for "Actually ..." utterances. No functional impact was demonstrated, and
   the complete/wait decision itself was correct. It is recorded, not fixed.
5. **Speech compatibility is partial.** One synthetic phrase round-tripped
   through the real relay, but recognition changed capitalization, so the exact
   transcription check is false (see Measured: speech).
6. **Final-tree full suite.** See Tests. The current tree has no recorded full
   `python check.py` run; that combined gate is a later step.

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
| correction flag | **failed** (metadata only) |

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
| M3 voice, turns, playback | semantic pause, decision fencing, owner/epoch playback ledger, Unicode partial prefix, actual-page probes | combined run at `0565a48`: 262 Python, 149 Node, Chromium. Independent review `OPUS-M3.json`: **not accepted** (F1, F2 above; F3 was a fixture race, fixed in `0565a48`) |
| M4 durability | SQLite ledger, restart recovery, jobs, continuity, byte bounds | `SOL-M4.json`; see Tests |

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
  recovery/state/controller/store tests and 56 with the tools tests. A full-suite
  run on that tree, and on this branch's tree, is not recorded.
- The last full tested tree in the run state is `394c0fa2`, not this branch.
- This documentation commit changes no code and ran no tests. A final combined
  `python check.py` is a later step. Run it with the dependencies in
  `requirements-dev.txt`.

## Isolated checkout smoke

The README's isolated-run commands were run against a disposable tmux server with
no credentials. Evidence is in `SONNET-M5.json`: loopback bind, an explicit port and
run directory, the Jarvis state endpoint, creation of the SQLite file, and clean
shutdown. This is a startup smoke test, not feature verification, and it used no
existing conversation.

## Unverified

- Physical phone microphone permission, acoustics, speaker output, mobile
  networking, and background or screen-lock behavior.
- End-to-end latency from speech to spoken answer. Component timings above are
  separate and are not summed.
- Non-ASCII provider audio alignment, long real conversations, and accuracy of the
  continuity classifier on real history.
- Behavior with existing real Codex and Claude conversations, which were not used.
- Any service restart or cutover. None was performed.

## Artifacts

All names are in the private run directory unless noted.

- Provider: `SOL-M5-PROVIDERS.json`, `SOL-M5-AUTH-RECHECK.json`,
  `SOL-M5-SPEECH.json`, `M5-PROVIDER-FINDINGS.json`, `SONNET-M5-AUTH.json`
- Stages: `M1-GATE.json`, `M2-GATE.json`, `OPUS-M3.json`,
  `M3-REVIEW-INTEGRATION-NOTE.json`, `SOL-M4.json`
- Live Haiku: `haiku-probe-fixes.json`, `core-scenario-live.json`
- This documentation: `SONNET-M5.json`
- Repository: `README.md`, `ARCHITECTURE.md`
