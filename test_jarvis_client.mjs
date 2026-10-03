import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const read = name => readFileSync(new URL(name, import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const json = (body, status = 200) => ({ok: status < 400, status, headers: {get: () => 'application/json'}, json: async () => body});

function ndjson(events) {
  const encoder = new TextEncoder();
  const queue = [...events];
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  return {
    release,
    response: {
      ok: true, status: 200, headers: {get: () => 'application/x-ndjson'},
      body: {getReader: () => ({
        read: async () => {
          if (!queue.length) return {done: true};
          const next = queue.shift();
          if (next === 'GATE') { await gate; return {value: new Uint8Array(), done: false}; }
          return {value: encoder.encode(JSON.stringify(next) + '\n'), done: false};
        },
        releaseLock() {},
      })},
    },
  };
}

function snapshot(overrides = {}) {
  return {session_id: 's1', revision: 1, subject: null, turns: {}, requests: {}, drafts: {}, actions: {}, jobs: {},
    playbacks: {}, clarifications: {}, ...overrides};
}
function turn(id, request, status, extra = {}) {
  return {turn_id: id, request_id: request, user_text: 'say hi', source: 'typed', mode: 'jarvis', status, ...extra};
}

function setup({state = snapshot(), post, now = {t: 0}} = {}) {
  const context = vm.createContext({TextDecoder, TextEncoder, AbortController, setTimeout, clearTimeout});
  vm.runInContext(read('readback.js') + read('jarvis.js') + '\nglobalThis.JarvisClient = JarvisClient; globalThis.JARVIS_TARGET = JARVIS_TARGET;', context);
  const calls = [];
  const records = new Map();
  const spoken = [];
  const presented = [];
  const players = [];
  const replyCalls = [];
  const voice = {
    generation: 0, capture: {ready: true},
    stopReadback() { for (const player of players) player.stopped = true; return ++this.generation; },
    speak(text, generation) { if (generation === this.generation) spoken.push(text); },
    speakReply(text, generation, spokenReady, onHeard) {
      if (generation !== this.generation) return;
      spoken.push(text);
      replyCalls.push({text, spokenReady, onHeard});
    },
    presentReply(text) { presented.push(text); },
    beginReadback(generation) {
      if (generation !== this.generation) return null;
      const player = {writes: [], ended: false, stopped: false, write(text) { if (!this.stopped) this.writes.push(text); },
        end() { this.ended = true; }, stop() { this.stopped = true; }};
      players.push(player);
      return player;
    },
  };
  let ids = 0;
  const env = {state};
  const fetch = async (url, options = {}) => {
    const call = {url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined, signal: options.signal};
    calls.push(call);
    if (url === '/api/jarvis/state') return env.state instanceof Error ? Promise.reject(env.state) : json(env.state);
    if (url === '/api/jarvis/cancel') return json({});
    if (url === '/api/replies/heard') return env.heard ? env.heard(call) : json({});
    if (url === '/api/jarvis/turn') return post(call);
    throw new Error('unexpected ' + url);
  };
  const changes = [];
  const client = new context.JarvisClient({fetch, newId: () => 'r' + ++ids, now: () => now.t, deadlineMs: 1000, lostGraceMs: 100,
    retain: record => records.set(record.id, {...records.get(record.id), ...record}), onChange: message => changes.push(message)});
  client.voice = voice;
  return {client, calls, records, spoken, replyCalls, presented, players, voice, env, now, changes,
    posts: () => calls.filter(call => call.url === '/api/jarvis/turn')};
}

const receipt = (request, text, extra = {}) => ({session_id: 's1', turn_id: 't-' + request, request_id: request, revision: 2,
  status: 'completed', text, actions: [], ...extra});

test('default input posts one turn to the Jarvis endpoint with ids, source and no model context', async () => {
  const s = setup({post: async () => json(receipt('r1', 'Hello.'))});
  await s.client.submit('say hi', 'typed');
  const [post] = s.posts();
  assert.deepEqual(Object.keys(post.body).sort(), ['request_id', 'session_id', 'source', 'stream', 'text']);
  assert.equal(post.body.session_id, 's1');
  assert.equal(post.body.request_id, 'r1');
  assert.equal(post.body.text, 'say hi');
  assert.equal(post.body.source, 'typed');
  assert.equal(s.calls.some(call => call.url === '/api/request'), false);
  assert.deepEqual(s.spoken, ['Hello.']);
  assert.equal(s.records.get('r1').reply.text, 'Hello.');
  assert.equal(s.client.pending.size, 0);
});

test('the Jarvis target is constant so pane selection cannot hold or retarget a Jarvis draft', () => {
  const s = setup({post: async () => json({})});
  const before = JSON.stringify(s.client.target);
  s.client.sessionId = 'other';
  assert.equal(JSON.stringify(s.client.target), before);
  assert.equal(Object.isFrozen(s.client.target), true);
});

test('a second request is accepted while the first is slow and only the latest speaks', async () => {
  const gates = {};
  const s = setup({post: call => new Promise(resolve => { gates[call.body.request_id] = resolve; })});
  const first = s.client.submit('first', 'typed');
  await tick();
  const second = s.client.submit('second', 'typed');
  await tick();
  assert.deepEqual(s.posts().map(call => call.body.request_id), ['r1', 'r2']);
  assert.equal(s.client.pending.size, 2);
  gates.r2(json(receipt('r2', 'Second answer.')));
  await second;
  gates.r1(json(receipt('r1', 'First answer.', {status: 'cancelled'})));
  await first;
  assert.deepEqual(s.spoken, ['Second answer.']);
  assert.match(s.records.get('r1').status, /Superseded/);
  assert.equal(s.client.pending.size, 0);
});

test('a superseded generation stays visible but never speaks', async () => {
  let resolve;
  const s = setup({post: () => new Promise(done => { resolve = done; })});
  const request = s.client.submit('first', 'typed');
  await tick();
  s.voice.stopReadback();
  resolve(json(receipt('r1', 'Late but real answer.')));
  await request;
  assert.deepEqual(s.spoken, []);
  assert.equal(s.records.get('r1').reply.text, 'Late but real answer.');
});

test('a late stream for a superseded request writes no more audio', async () => {
  const slow = ndjson([{type: 'text', text: 'Working on '}, 'GATE', {type: 'text', text: 'it.'}, {type: 'result', result: receipt('r1', 'it.')}]);
  const s = setup({post: call => call.body.request_id === 'r1' ? slow.response : json(receipt('r2', 'Newer.'))});
  const first = s.client.submit('first', 'typed');
  await tick(); await tick();
  assert.deepEqual(s.players[0].writes, ['Working on ']);
  await s.client.submit('second', 'typed');
  assert.equal(s.players[0].stopped, true);
  slow.release();
  await first;
  assert.deepEqual(s.players[0].writes, ['Working on ']);
  assert.deepEqual(s.spoken, ['Newer.']);
});

test('streamed narration plus final text is completed without replaying what was heard', async () => {
  const stream = ndjson([{type: 'text', text: 'Checking. '}, {type: 'text', text: 'It is done.'},
    {type: 'result', result: receipt('r1', 'It is done.')}]);
  const s = setup({post: () => stream.response});
  await s.client.submit('go', 'voice');
  assert.deepEqual(s.players[0].writes, ['Checking. ', 'It is done.']);
  assert.equal(s.players[0].ended, true);
  assert.equal(s.presented.at(-1), 'It is done.');
});

test('a lost response recovers the receipt from state by request id and never re-posts', async () => {
  const s = setup({post: async () => { throw new Error('network'); }});
  const request = s.client.submit('say hi', 'typed');
  await tick(); await tick();
  assert.equal(s.client.pending.size, 1);
  s.env.state = snapshot({revision: 2, requests: {r1: 't1'},
    turns: {t1: turn('t1', 'r1', 'completed', {result: {text: 'Recovered.'}})}});
  await s.client.refresh();
  const result = await request;
  assert.equal(result.text, 'Recovered.');
  assert.equal(s.posts().length, 1);
  assert.deepEqual(s.spoken, ['Recovered.']);
});

test('a lost response with no state record is reported unconfirmed after the grace period, not resent', async () => {
  const s = setup({post: async () => { throw new Error('network'); }});
  const request = s.client.submit('say hi', 'typed');
  await tick(); await tick();
  s.now.t = 50;
  await s.client.refresh();
  assert.equal(s.client.pending.size, 1);
  s.now.t = 500;
  await s.client.refresh();
  assert.equal(await request, null);
  assert.equal(s.posts().length, 1);
  assert.match(s.records.get('r1').status, /Outcome unknown.*Check Jarvis activity before repeating/);
  assert.match(s.spoken[0], /Nothing was resent/);
});

test('a running turn keeps the request pending while state is polled', async () => {
  const s = setup({post: async () => { throw new Error('network'); }});
  s.client.submit('say hi', 'typed');
  await tick(); await tick();
  s.env.state = snapshot({revision: 2, requests: {r1: 't1'}, turns: {t1: turn('t1', 'r1', 'running')}});
  await s.client.refresh();
  assert.equal(s.client.pending.size, 1);
  assert.equal(s.posts().length, 1);
});

test('a server restart cannot match pending requests and settles them without resending', async () => {
  const s = setup({post: () => new Promise(() => {})});
  const request = s.client.submit('say hi', 'typed');
  await tick();
  s.env.state = snapshot({session_id: 's2'});
  await s.client.refresh();
  assert.equal(await request, null);
  assert.equal(s.posts().length, 1);
  assert.match(s.records.get('r1').status, /restarted/);
});

test('input is not sent and stays recoverable when Jarvis state is unavailable', async () => {
  const s = setup({state: new Error('down'), post: async () => json({})});
  assert.equal(await s.client.submit('say hi', 'typed'), null);
  assert.equal(s.posts().length, 0);
  assert.equal(s.records.get('r1').recoverable, true);
  assert.equal(s.records.get('r1').text, 'say hi');
});

test('a rejected request keeps the words recoverable and does not speak a reply', async () => {
  const s = setup({post: async () => json({error: 'bad session'}, 409)});
  await s.client.submit('say hi', 'typed');
  assert.equal(s.records.get('r1').recoverable, true);
  assert.match(s.records.get('r1').status, /rejected.*bad session/);
  assert.equal(s.posts().length, 1);
});

test('state renders subject, jobs and action receipts; submitted is never shown as completed', async () => {
  const state = snapshot({
    revision: 5, subject: {tab: 'dog', pane: 'p1', identity: 'i1', label: 'Dog Data'},
    requests: {r9: 't9'},
    turns: {t9: turn('t9', 'r9', 'completed', {result: {text: 'Sent it.'}})},
    actions: {a1: {action_id: 'a1', turn_id: 't9', tool: 'send_draft', draft_id: 'd1', payload: 'exact words', status: 'submitted', target: {label: 'Dog Data'}},
      a2: {action_id: 'a2', turn_id: 't9', tool: 'send_draft', draft_id: 'd2', payload: 'x', status: 'uncertain', target: {label: 'Dog Data'}}},
    jobs: {j1: {job_id: 'j1', turn_id: 't9', status: 'running', subject: {label: 'Dog Data'}}},
  });
  const s = setup({state, post: async () => json({})});
  await s.client.refresh();
  assert.equal(s.client.summary.subject, 'Dog Data');
  assert.deepEqual(JSON.parse(JSON.stringify(s.client.summary.jobs)), [{label: 'Dog Data', status: 'running'}]);
  const record = s.records.get('r9');
  assert.match(record.status, /Submitted to Dog Data\. Submitted is not completed/);
  assert.match(record.status, /uncertain\. Check it before sending again/);
  assert.match(record.status, /Job · Dog Data: running/);
  assert.equal(record.deliveredText, 'exact words');
  assert.equal(record.reply.text, 'Sent it.');
});

test('background job notices and replayed state never speak', async () => {
  const s = setup({post: async () => json({})});
  await s.client.refresh();
  s.env.state = snapshot({revision: 2, requests: {r9: 't9'}, turns: {t9: turn('t9', 'r9', 'completed', {result: {text: 'Old answer.'}})},
    jobs: {j1: {job_id: 'j1', turn_id: 't9', status: 'completed', subject: {label: 'Dog Data'}}}});
  await s.client.refresh();
  assert.deepEqual(s.spoken, []);
  assert.equal(s.players.length, 0);
});

test('stale state snapshots with a lower revision are ignored', async () => {
  const s = setup({state: snapshot({revision: 4, subject: {tab: 'a', label: 'New'}}), post: async () => json({})});
  await s.client.refresh();
  s.env.state = snapshot({revision: 2, subject: {tab: 'b', label: 'Old'}});
  await s.client.refresh();
  assert.equal(s.client.summary.subject, 'New');
});

test('a direct submission cancels Jarvis inference only and the cancelled turn cannot speak', async () => {
  let resolve;
  const s = setup({post: () => new Promise(done => { resolve = done; })});
  const request = s.client.submit('first', 'typed');
  await tick();
  await s.client.supersedeForDirect();
  const cancel = s.calls.find(call => call.url === '/api/jarvis/cancel');
  assert.deepEqual(cancel.body, {session_id: 's1', request_id: 'r1'});
  s.voice.stopReadback();
  resolve(json(receipt('r1', 'Too late.')));
  await request;
  assert.deepEqual(s.spoken, []);
});

test('the request deadline reports an unconfirmed outcome without resending', async () => {
  const s = setup({post: () => new Promise(() => {})});
  s.client.deadlineMs = 5;
  assert.equal(await s.client.submit('say hi', 'typed'), null);
  assert.equal(s.posts().length, 1);
  assert.match(s.records.get('r1').status, /Outcome unknown/);
});

test('a failed turn shows the error and keeps its receipts without fabricating completion', async () => {
  const failed = receipt('r1', '', {status: 'failed', error: 'provider down',
    actions: [{tool: 'send_draft', draft_id: 'd1', status: 'uncertain', target: {label: 'Dog Data'}}]});
  const s = setup({post: async () => json(failed)});
  await s.client.submit('say hi', 'typed');
  const status = s.records.get('r1').status;
  assert.match(status, /could not complete this: provider down/);
  assert.match(status, /uncertain/);
  assert.equal(s.records.get('r1').reply, undefined);
});

const REF = {id: 'att-1', tab: '@b', pane: '%b', identity: 'b:original', label: 'B · pane 1'};
const READBACK = {original: 'Exact  original\nreply — with “quotes”.', target: {tab: '@b', pane: '%b', identity: 'b:original', label: 'B · pane 1'}, reply_ref: REF};
const heardPosts = s => s.calls.filter(call => call.url === '/api/replies/heard');

test('an immediate readback receipt speaks the exact original through speakReply and keeps it visible', async () => {
  const s = setup({post: async () => json(receipt('r1', 'Reading B.', {readback: READBACK, raw_model_text: 'RAW PRE-TOOL NARRATION'}))});
  await s.client.submit('read B', 'voice');
  assert.equal(s.replyCalls.length, 1);
  assert.equal(s.replyCalls[0].text, READBACK.original);
  assert.equal(s.replyCalls[0].spokenReady, false);
  assert.equal(typeof s.replyCalls[0].onHeard, 'function');
  assert.deepEqual(s.spoken, [READBACK.original]);
  assert.equal(s.players.length, 0);
  const record = s.records.get('r1');
  assert.equal(record.reply.text, READBACK.original);
  assert.equal(record.reply.target, 'B · pane 1');
  assert.deepEqual(record.reply.heard_reply, REF);
  assert.match(record.status, /Reading B\./);
  assert.equal(JSON.stringify([...s.records.values()]).includes('RAW PRE-TOOL'), false);
  assert.equal(heardPosts(s).length, 0);
});

test('the heard acknowledgment posts the exact reference only when full audio completes', async () => {
  const s = setup({post: async () => json(receipt('r1', 'Reading B.', {readback: READBACK}))});
  await s.client.submit('read B', 'voice');
  assert.equal(heardPosts(s).length, 0);
  s.replyCalls[0].onHeard();
  await tick();
  const [heard] = heardPosts(s);
  assert.equal(heard.method, 'POST');
  assert.deepEqual(heard.body, REF);
});

test('a readback without an exact reference is read but can never be marked heard', async () => {
  const s = setup({post: async () => json(receipt('r1', 'Reading B.', {readback: {...READBACK, reply_ref: null}}))});
  await s.client.submit('read B', 'voice');
  assert.equal(s.replyCalls[0].text, READBACK.original);
  assert.equal(s.replyCalls[0].onHeard, null);
  assert.equal(s.records.get('r1').reply.heard_reply, null);
  assert.equal(heardPosts(s).length, 0);
});

test('a superseded or stopped generation never speaks a readback or marks it heard, but keeps the original', async () => {
  let resolve;
  const s = setup({post: () => new Promise(done => { resolve = done; })});
  const request = s.client.submit('read B', 'voice');
  await tick();
  s.voice.stopReadback();
  resolve(json(receipt('r1', 'Reading B.', {readback: READBACK})));
  await request;
  assert.equal(s.replyCalls.length, 0);
  assert.deepEqual(s.spoken, []);
  assert.equal(heardPosts(s).length, 0);
  assert.equal(s.records.get('r1').reply.text, READBACK.original);
  assert.deepEqual(s.records.get('r1').reply.heard_reply, REF);
});

test('a readback that is not the latest request never speaks even when its generation is current', async () => {
  const gates = {};
  const s = setup({post: call => new Promise(resolve => { gates[call.body.request_id] = resolve; })});
  const first = s.client.submit('read B', 'voice');
  await tick();
  const second = s.client.submit('next', 'voice');
  await tick();
  gates.r1(json(receipt('r1', 'Reading B.', {readback: READBACK})));
  await first;
  assert.equal(s.replyCalls.length, 0);
  assert.equal(heardPosts(s).length, 0);
  gates.r2(json(receipt('r2', 'Next.')));
  await second;
  assert.deepEqual(s.spoken, ['Next.']);
});

test('a lost POST recovers canonical text and readback from state, speaks once, and never re-posts', async () => {
  const s = setup({post: async () => { throw new Error('network'); }});
  const request = s.client.submit('read B', 'voice');
  await tick(); await tick();
  s.env.state = snapshot({revision: 2, requests: {r1: 't1'}, turns: {t1: turn('t1', 'r1', 'completed',
    {result: {text: 'Reading B.', readback: READBACK, raw_model_text: 'RAW'}})}});
  await s.client.refresh();
  const result = await request;
  assert.equal(result.text, 'Reading B.');
  assert.deepEqual(JSON.parse(JSON.stringify(result.readback)), READBACK);
  assert.equal(s.posts().length, 1);
  assert.equal(s.replyCalls.length, 1);
  assert.equal(s.replyCalls[0].text, READBACK.original);
  assert.deepEqual(s.records.get('r1').reply.heard_reply, REF);
  s.replyCalls[0].onHeard();
  await tick();
  assert.deepEqual(heardPosts(s).map(call => call.body), [REF]);
  await s.client.refresh();
  assert.equal(s.replyCalls.length, 1);
});

test('replayed state with readback never speaks or marks heard', async () => {
  const s = setup({post: async () => json({})});
  s.env.state = snapshot({revision: 2, requests: {r9: 't9'}, turns: {t9: turn('t9', 'r9', 'completed',
    {result: {text: 'Reading B.', readback: READBACK}})}});
  await s.client.refresh();
  await s.client.refresh();
  assert.equal(s.replyCalls.length, 0);
  assert.equal(heardPosts(s).length, 0);
  assert.equal(s.records.get('r9').reply.text, READBACK.original);
  assert.deepEqual(s.records.get('r9').reply.heard_reply, REF);
});

test('a failed heard acknowledgment is reported and never retried', async () => {
  const s = setup({post: async () => json(receipt('r1', 'Reading B.', {readback: READBACK}))});
  s.env.heard = async () => json({error: 'identity changed'}, 409);
  await s.client.submit('read B', 'voice');
  s.replyCalls[0].onHeard();
  await tick(); await tick();
  assert.equal(heardPosts(s).length, 1);
  assert.match(s.changes.at(-1), /Could not record/);
});

test('automatic cancellation names only the requests this client owns', async () => {
  const gates = {};
  const s = setup({post: call => new Promise(resolve => { gates[call.body.request_id] = resolve; })});
  const first = s.client.submit('first', 'typed');
  await tick();
  const second = s.client.submit('second', 'typed');
  await tick();
  gates.r1(json(receipt('r1', 'First.', {status: 'cancelled'})));
  await first;
  await s.client.supersedeForDirect();
  const cancels = s.calls.filter(call => call.url === '/api/jarvis/cancel');
  assert.deepEqual(cancels.map(call => call.body), [{session_id: 's1', request_id: 'r2'}]);
  gates.r2(json(receipt('r2', 'Too late.', {status: 'cancelled'})));
  await second;
});

test('with nothing owned and pending, direct submission sends no cancellation and a stale response cancels nothing', async () => {
  const s = setup({post: async () => json(receipt('r1', 'Done.'))});
  await s.client.submit('first', 'typed');
  await s.client.supersedeForDirect();
  assert.equal(s.calls.some(call => call.url === '/api/jarvis/cancel'), false);
});
