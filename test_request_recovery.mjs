import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
const submit = html.slice(html.indexOf('    async function submitRequest'), html.indexOf("    $('terminal-toggle').onclick"));
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup(fetch, refresh = () => new Promise(() => {})) {
  const elements = new Map(['transcript', 'result'].map(id => [id, {}]));
  const records = new Map();
  const restored = [];
  const context = vm.createContext({
    $, fetch, refresh, AbortController, setTimeout, clearTimeout,
    readJSONStream: response => response.json(),
    retainSpeech(record) { records.set(record.id, {...records.get(record.id), ...record}); }, requestOutcome: result => result.error || result.action,
 crypto: {randomUUID: () => Math.random().toString()},
    voice: {stopReadback() { return 1; }, speak() {}, restoreDraft(...args) { restored.push(args); return true; }},
    currentState: null,
    retainOutcome(id, result) { context.retainSpeech({id, status: result.error || result.action, delivery: result.delivery}); },
    targetName: () => 'fixture',
  });
  function $(id) { return elements.get(id); }
  vm.runInContext(`let submitting = false; let pendingRequest = null;
    function setSubmitting(value) { submitting = value; }
    function showError(error) { $('result').textContent = error.message; }
    ${submit}
    globalThis.submit = submitRequest;
    globalThis.busy = () => submitting;
    globalThis.complete = event => pendingRequest.resolve(event);
  `, context);
  return {context, elements, records, restored};
}
const success = {timestamp: 'fixture', outcome: 'ok', action: 'no_action'};

test('completed POST releases busy state even when follow-up refresh never returns', async () => {
  let calls = 0;
  const s = setup(async () => { calls++; return {ok: true, json: async () => success}; });
  await s.context.submit('first');
  assert.equal(s.context.busy(), false);
  await s.context.submit('second');
  assert.equal(calls, 2);
});

test('poll completion releases busy state when POST response is lost', async () => {
  let signal;
  const s = setup((url, options) => { signal = options.signal; return new Promise(() => {}); });
  const request = s.context.submit('first');
  assert.equal(s.context.busy(), true);
  s.context.complete(success);
  await request;
  assert.equal(s.context.busy(), false);
  assert.equal(signal.aborted, true);
});

test('poll recovery completes partially streamed observer audio without replay or a second rewrite', async () => {
  const s = setup(async () => ({ok: true}));
  const audio = [];
  const player = {write: text => audio.push(text), end: () => audio.push('END'), stop() {}};
  s.context.voice = {generation: 1, capture: {ready: true}, stopReadback: () => 1,
    beginReadback: () => player, presentReply() {}, status() {}, speakReply() { throw new Error('Unexpected rewrite'); }};
  s.context.readJSONStream = (response, onText) => { onText('First. '); return new Promise(() => {}); };
  const request = s.context.submit('What is it doing?');
  await tick();
  assert.deepEqual(audio, ['First. ']);
  s.context.complete({...success, action: 'ask_session', output: 'First. Last.'});
  await request;
  assert.deepEqual(audio, ['First. ', 'Last.', 'END']);
});

test('provider error is displayed and busy clears without waiting for refresh', async () => {
  const s = setup(async () => ({ok: false, json: async () => ({timestamp: 'fixture', outcome: 'error', error: 'TypeSafe HTTP 503'})}));
  await s.context.submit('first');
  assert.equal(s.context.busy(), false);
  assert.equal(s.elements.get('result').textContent, 'TypeSafe HTTP 503');
});

test('unfinished request still prevents concurrent dispatch', async () => {
  let calls = 0;
  const s = setup(() => { calls++; return new Promise(() => {}); });
  const first = s.context.submit('first');
  await s.context.submit('second');
  assert.match(s.elements.get('result').textContent, /saved, but were not sent/);
  assert.equal(calls, 1);
  s.context.complete(success);
  await first;
  await tick();
});


test('deadline releases local busy state without declaring non-delivery or retrying', async () => {
  let calls = 0;
  const s = setup(() => { calls++; return new Promise(() => {}); });
  s.context.setTimeout = callback => setTimeout(callback, 5);
  await s.context.submit('first');
  assert.equal(s.context.busy(), false);
  assert.equal(calls, 1);
  const record = [...s.records.values()][0];
  assert.equal(record.text, 'first');
  assert.match(record.status, /Outcome unknown/);
  assert.doesNotMatch(record.status, /Nothing was sent/);
});

test('only confirmed not-attempted failures restore exact words and captured recipient', async () => {
  for (const delivery of ['not_attempted', 'submitted', 'uncertain', undefined]) {
    const s = setup(async () => ({ok: true, json: async () => ({timestamp: 'fixture', action: 'send_message', outcome: 'error', error: 'Recipient unavailable', delivery})}));
    const target = {tab: '@fixture', pane: '%fixture', identity: 'original-conversation'};
    await s.context.submit('Original words.', target);
    assert.equal(s.restored.length, delivery === 'not_attempted' ? 1 : 0);
    if (delivery === 'not_attempted') assert.deepEqual(s.restored[0], ['Original words.', target, 'Recipient unavailable']);
  }
});


test('typing and speech submit identical words and captured context through request receipts', async () => {
  const payloads = [];
  const s = setup(async (url, options) => {
    assert.equal(url, '/api/request');
    payloads.push(JSON.parse(options.body));
    return {ok: true, json: async () => ({...success, action: 'send_message', delivery: 'submitted'})};
  });
  const target = {tab: '@a', pane: '%a', identity: 'conversation-a'};
  for (const source of ['typed', 'voice']) await s.context.submit('Exact words.\nSecond line.', target, source);
  assert.equal(payloads.length, 2);
  assert.deepEqual(payloads.map(p => p.source), ['typed', 'voice']);
  for (const payload of payloads) {
    assert.equal(payload.request, 'Exact words.\nSecond line.');
    assert.deepEqual(payload.capture_target, target);
    assert.ok(payload.request_id);
  }
  assert.notEqual(payloads[0].request_id, payloads[1].request_id);
});
