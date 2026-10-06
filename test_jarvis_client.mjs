import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {createHash, webcrypto} from 'node:crypto';

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
  const context = vm.createContext({TextDecoder, TextEncoder, AbortController, setTimeout, clearTimeout, crypto: webcrypto});
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
    speakReply(text, generation, spokenReady, onHeard, track) {
      if (generation !== this.generation) return;
      spoken.push(text);
      replyCalls.push({text, spokenReady, onHeard, track});
    },
    presentReply(text) { presented.push(text); },
    beginReadback(generation) {
      if (generation !== this.generation) return null;
      const player = {writes: [], ended: false, stopped: false, trackers: [], track(callback) { this.trackers.push(callback); }, write(text) { if (!this.stopped) this.writes.push(text); },
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
    if (url === '/api/jarvis/turn-decision') return env.decision ? env.decision(call) : json({});
    if (url === '/api/jarvis/playback') return env.playback ? env.playback(call) : json({});
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
  assert.deepEqual(Object.keys(post.body).sort(), ['client_id', 'explicit_send', 'request_id', 'session_id', 'source', 'stream', 'text']);
  assert.equal(post.body.explicit_send, true);
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

test('a failed turn after a submitted or uncertain action speaks the canonical receipt text, not generic failure', async () => {
  for (const [status, text] of [['submitted', 'Input submitted to Dog Data. Waiting for its reply; its task is not complete.'],
      ['uncertain', 'Delivery to Dog Data is uncertain. It will not be retried automatically.']]) {
    const failed = receipt('r1', text, {status: 'failed', error: 'provider exited mid-turn', raw_model_text: 'All done, sent and finished!',
      actions: [{tool: 'send_draft', draft_id: 'd1', status, target: {label: 'Dog Data'}}]});
    const s = setup({post: async () => json(failed)});
    await s.client.submit('tell dog data hello', 'voice');
    assert.deepEqual(s.spoken, [text]);
    assert.equal(s.records.get('r1').reply, undefined);
    assert.doesNotMatch(s.spoken.join(' '), /All done|could not complete/);
    assert.equal(s.posts().length, 1);
  }
});

test('a failed turn without actions, or with blank receipt text, keeps the generic failure speech', async () => {
  for (const failed of [receipt('r1', 'Some text', {status: 'failed', error: 'down'}),
      receipt('r1', '  ', {status: 'failed', error: 'down', actions: [{tool: 'send_draft', draft_id: 'd1', status: 'failed'}]})]) {
    const s = setup({post: async () => json(failed)});
    await s.client.submit('say hi', 'typed');
    assert.deepEqual(s.spoken, ['Jarvis could not complete that request. The details are on screen.']);
  }
});

test('a superseded failed turn with a submitted action never speaks its receipt but stays visible', async () => {
  const failed = receipt('r1', 'Input submitted to Dog Data. Waiting for its reply; its task is not complete.', {status: 'failed', error: 'x',
    actions: [{tool: 'send_draft', draft_id: 'd1', status: 'submitted', target: {label: 'Dog Data'}}]});
  let resolve;
  const s = setup({post: () => new Promise(done => { resolve = done; })});
  const submitted = s.client.submit('tell dog data hello', 'voice');
  await tick();
  s.voice.stopReadback();
  resolve(json(failed));
  await submitted;
  assert.deepEqual(s.spoken, []);
  assert.match(s.records.get('r1').status, /Submitted to Dog Data/);
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
  assert.equal(s.replyCalls[0].onHeard, null);
  assert.equal(typeof s.replyCalls[0].track, 'function');
  assert.equal(typeof s.replyCalls[0].track.prepare, 'function');
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

test('tracked Jarvis audio has no direct heard callback; only a valid completed playback can acknowledge it', async () => {
  const s = setup({post: async () => json(receipt('r1', 'Reading B.', {readback: READBACK}))});
  await s.client.submit('read B', 'voice');
  assert.equal(s.replyCalls[0].onHeard, null);
  s.env.playback = fullPlayback();
  const p = player();
  s.replyCalls[0].track(p, {style: 'original'});
  p.callback({status: 'playing'});
  p.callback({status: 'completed'});
  await tick(); await tick();
  assert.equal(heardPosts(s).length, 0);
  const updates = playbackPosts(s).filter(body => body.op === 'update');
  assert.deepEqual(updates.map(body => [body.status, body.completed_phrases]), [['playing', []], ['completed', ['fb1']]]);
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
  assert.equal(s.replyCalls[0].onHeard, null);
  assert.equal(heardPosts(s).length, 0);
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

test('a rejected completed playback update is never retried and falls back to no direct heard claim', async () => {
  const s = setup({post: async () => json(receipt('r1', 'Reading B.', {readback: READBACK}))});
  await s.client.submit('read B', 'voice');
  s.env.playback = call => call.body.op === 'create' ? fullPlayback()(call) : json({error: 'stale owner'}, 409);
  const p = player();
  s.replyCalls[0].track(p, {style: 'original'});
  p.callback({status: 'playing'});
  p.callback({status: 'completed'});
  await tick(); await tick(); await tick();
  assert.deepEqual(playbackPosts(s).map(body => body.op + ':' + (body.status || '')), ['create:', 'update:playing']);
  assert.equal(heardPosts(s).length, 0);
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

const decisionResult = (sent, extra = {}) => ({session_id: sent.session_id, client_id: sent.client_id, transcript_id: sent.transcript_id,
  transcript_revision: sent.transcript_revision, subject: sent.subject, decision: 'complete', decision_id: 'dec-1', ...extra});
const plain = value => JSON.parse(JSON.stringify(value));
const decide = (s, extra = {}, mutate = {}) => {
  s.env.decision = call => json({...decisionResult(call.body, extra), ...mutate});
  return s.client.semantic.decide({transcript_id: 'x1', transcript_revision: 3, text: 'Tell Dog Data hello'}, undefined);
};

test('turn decisions send the app subject, never a pane, and accept only a current complete token', async () => {
  const subject = {tab: 'dog', label: 'Dog Data'};
  const s = setup({state: snapshot({subject}), post: async () => json({})});
  await s.client.refresh();
  assert.deepEqual(plain(await decide(s, {correction: true})), {decision: 'complete', decision_id: 'dec-1', correction: true});
  const call = s.calls.find(item => item.url === '/api/jarvis/turn-decision');
  assert.deepEqual(call.body, {session_id: 's1', client_id: s.client.clientId, transcript_id: 'x1', transcript_revision: 3,
    text: 'Tell Dog Data hello', subject});
  assert.equal(call.method, 'POST');
});

test('wait, unknown and tokenless decisions all hold; cancel is surfaced', async () => {
  const s = setup({post: async () => json({})});
  await s.client.refresh();
  assert.equal((await decide(s, {decision: 'wait'})).decision, 'wait');
  assert.equal((await decide(s, {decision: 'send_now'})).decision, 'wait');
  assert.equal((await decide(s, {}, {decision_id: undefined})).decision, 'wait');
  assert.equal((await decide(s, {}, {decision_id: ''})).decision, 'wait');
  assert.equal((await decide(s, {decision: 'cancel'})).decision, 'cancel');
});

test('a decision for another session, client, transcript, revision or subject is stale and never complete', async () => {
  const s = setup({state: snapshot({subject: {tab: 'dog'}}), post: async () => json({})});
  await s.client.refresh();
  for (const mutate of [{session_id: 'other'}, {client_id: 'other'}, {transcript_id: 'other'}, {transcript_revision: 2},
    {subject: {tab: 'cat'}}, {subject: null}]) {
    const result = await decide(s, {}, mutate);
    assert.deepEqual(plain(result), {decision: 'wait', stale: true}, JSON.stringify(mutate));
  }
});

test('a subject change while the decision was in flight makes the answer stale', async () => {
  const s = setup({state: snapshot({subject: {tab: 'dog'}}), post: async () => json({})});
  await s.client.refresh();
  s.env.decision = call => {
    s.env.state = snapshot({revision: 2, subject: {tab: 'cat'}});
    return s.client.refresh().then(() => json(decisionResult(call.body)));
  };
  const result = await s.client.semantic.decide({transcript_id: 'x1', transcript_revision: 3, text: 'hello'});
  assert.deepEqual(plain(result), {decision: 'wait', stale: true});
});

test('a failed decision request rejects so the voice layer holds the words', async () => {
  const s = setup({post: async () => json({})});
  await s.client.refresh();
  s.env.decision = () => json({error: 'down'}, 503);
  await assert.rejects(s.client.semantic.decide({transcript_id: 'x1', transcript_revision: 1, text: 'hi'}), /down/);
  const fresh = setup({post: async () => json({})});
  await assert.rejects(fresh.client.semantic.decide({transcript_id: 'x1', transcript_revision: 1, text: 'hi'}), /unavailable/);
});

test('invalidation posts invalidate=true once and ignores its own failure', async () => {
  const s = setup({post: async () => json({})});
  await s.client.refresh();
  s.env.decision = () => { throw new Error('offline'); };
  s.client.semantic.invalidate({transcript_id: 'x1', transcript_revision: 4, text: 'more'});
  await tick();
  const calls = s.calls.filter(call => call.url === '/api/jarvis/turn-decision');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.invalidate, true);
  assert.equal(calls[0].body.transcript_revision, 4);
});

test('automatic voice submit carries the decision token; explicit and typed submit carry explicit_send', async () => {
  const s = setup({post: async call => json(receipt(call.body.request_id, 'Done.'))});
  await s.client.submit('go', 'voice', {transcript_id: 'x1', transcript_revision: 5, decision_id: 'dec-9'});
  await s.client.submit('go again', 'voice', {transcript_id: 'x2', transcript_revision: 2, explicit_send: true});
  await s.client.submit('typed words', 'typed');
  const [auto, explicit, typed] = s.posts().map(call => call.body);
  assert.equal(auto.decision_id, 'dec-9');
  assert.equal('explicit_send' in auto, false);
  assert.equal(auto.transcript_id, 'x1');
  assert.equal(auto.transcript_revision, 5);
  assert.equal(explicit.explicit_send, true);
  assert.equal('decision_id' in explicit, false);
  assert.equal(typed.explicit_send, true);
  assert.equal('transcript_id' in typed, false);
  assert.equal(auto.client_id, s.client.clientId);
});

const fullPlayback = (extra = {}) => call => call.body.op === 'create'
  ? json({playback_id: 'pb1', turn_id: call.body.turn_id, client_id: call.body.client_id, epoch: call.body.epoch,
    full_boundary_id: 'fb1', status: 'generated', ...extra})
  : json({playback_id: call.body.playback_id, status: call.body.status});
const playbackPosts = s => s.calls.filter(call => call.url === '/api/jarvis/playback').map(call => call.body);
const player = progress => ({progress, track(callback) { this.callback = callback; }});

test('playback is created lazily and completed only with the app full boundary after proven full audio', async () => {
  const s = setup({post: async () => json({})});
  await s.client.refresh();
  s.env.playback = fullPlayback();
  const p = player();
  s.client.trackPlayback(p, {generation: 7, turnId: 't1', style: 'spoken'});
  p.callback({status: 'generated'});
  await tick();
  assert.deepEqual(playbackPosts(s), []);
  p.callback({status: 'playing', completed_phrases: []});
  p.callback({status: 'completed', completed_phrases: ['p1', 'p2']});
  await tick(); await tick();
  const [create, playing, completed] = playbackPosts(s);
  assert.deepEqual(create, {session_id: 's1', op: 'create', turn_id: 't1', client_id: s.client.clientId, epoch: 7, style: 'spoken'});
  assert.deepEqual([playing.op, playing.status, playing.completed_phrases], ['update', 'playing', []]);
  assert.deepEqual([completed.status, completed.completed_phrases, completed.playback_id, completed.epoch], ['completed', ['fb1'], 'pb1', 7]);
});

test('cancelled and failed playback report no completed boundary and nothing after the terminal state', async () => {
  for (const status of ['cancelled', 'failed']) {
    const s = setup({post: async () => json({})});
    await s.client.refresh();
    s.env.playback = fullPlayback();
    const p = player();
    s.client.trackPlayback(p, {generation: 2, turnId: 't1', style: 'original'});
    p.callback({status: 'playing'});
    p.callback({status});
    p.callback({status: 'completed'});
    await tick(); await tick();
    const updates = playbackPosts(s).filter(body => body.op === 'update');
    assert.deepEqual(updates.map(body => body.status), ['playing', status]);
    assert.ok(updates.every(body => body.completed_phrases.length === 0));
  }
});

test('a rejected or mismatched playback record kills the ledger without retry or a heard claim', async () => {
  for (const respond of [() => json({error: 'no'}, 409), fullPlayback({epoch: 99}), fullPlayback({client_id: 'other'}), fullPlayback({playback_id: 5})]) {
    const s = setup({post: async () => json({})});
    await s.client.refresh();
    s.env.playback = respond;
    const p = player();
    s.client.trackPlayback(p, {generation: 3, turnId: 't1', style: 'spoken'});
    p.callback({status: 'playing'});
    p.callback({status: 'completed'});
    await tick(); await tick();
    assert.deepEqual(playbackPosts(s).map(body => body.op), ['create']);
    assert.equal(heardPosts(s).length, 0);
  }
});

test('a completed playback without an app boundary is reported failed, never completed', async () => {
  const s = setup({post: async () => json({})});
  await s.client.refresh();
  s.env.playback = fullPlayback({full_boundary_id: undefined});
  const p = player();
  s.client.trackPlayback(p, {generation: 3, turnId: 't1', style: 'spoken'});
  p.callback({status: 'completed'});
  await tick(); await tick();
  const update = playbackPosts(s).find(body => body.op === 'update');
  assert.equal(update.status, 'failed');
  assert.deepEqual(update.completed_phrases, []);
});

test('a playback update that arrives after the server session changed is not posted', async () => {
  const s = setup({post: async () => json({})});
  await s.client.refresh();
  s.env.playback = fullPlayback();
  const p = player();
  s.client.trackPlayback(p, {generation: 3, turnId: 't1', style: 'spoken'});
  s.env.state = snapshot({session_id: 's2', revision: 1});
  await s.client.refresh();
  p.callback({status: 'completed'});
  await tick(); await tick();
  assert.deepEqual(playbackPosts(s), []);
});

test('readback and non-streamed replies hand the voice layer a tracker bound to the turn and generation', async () => {
  const s = setup({post: async () => json(receipt('r1', 'Reading B.', {readback: READBACK}))});
  await s.client.refresh();
  s.env.playback = fullPlayback();
  await s.client.submit('read B', 'voice');
  const p = player();
  s.replyCalls[0].track(p, {style: 'spoken'});
  p.callback({status: 'playing'});
  await tick(); await tick();
  assert.deepEqual(playbackPosts(s)[0], {session_id: 's1', op: 'create', turn_id: 't-r1', client_id: s.client.clientId,
    epoch: s.voice.generation, style: 'spoken'});
});

test('streamed narration is tracked only when it is an exact prefix of the final reply', async () => {
  for (const [spokenText, tracked] of [['Hello ', true], ['Hallo ', false]]) {
    const stream = ndjson([{type: 'text', text: spokenText}, 'GATE', {type: 'result', result: receipt('r1', 'Hello there.')}]);
    const s = setup({post: async () => stream.response});
    await s.client.refresh();
    s.env.playback = fullPlayback();
    const submitting = s.client.submit('hi', 'voice');
    await tick();
    stream.release();
    await submitting;
    const [streamPlayer] = s.players;
    assert.equal(streamPlayer.ended, true);
    assert.equal(streamPlayer.trackers.length, tracked ? 1 : 0, spokenText);
    assert.equal(streamPlayer.writes.join(''), tracked ? 'Hello there.' : 'Hallo  Hello there.');
  }
});

test('stopping readback never cancels, sends or posts a turn and a later receipt cannot restart speech', async () => {
  let resolve;
  const s = setup({post: () => new Promise(done => { resolve = done; })});
  const submitting = s.client.submit('read B', 'voice');
  await tick();
  const callsBefore = s.calls.length;
  s.voice.stopReadback('stop');
  assert.equal(s.calls.length, callsBefore);
  resolve(json(receipt('r1', 'Reading B.', {readback: READBACK})));
  await submitting;
  assert.deepEqual(s.spoken, []);
  assert.equal(s.replyCalls.length, 0);
  assert.equal(heardPosts(s).length, 0);
  assert.equal(s.calls.some(call => call.url === '/api/jarvis/cancel' || call.url === '/api/jarvis/playback'), false);
  assert.equal(s.records.get('r1').reply.text, READBACK.original);
});

test('reconnect replay with completed turns and finished jobs posts no playback, heard or speech', async () => {
  const s = setup({post: async () => json({})});
  s.env.playback = fullPlayback();
  s.env.state = snapshot({revision: 2, requests: {r9: 't9'}, turns: {t9: turn('t9', 'r9', 'completed', {result: {text: 'Reading B.', readback: READBACK}})},
    playbacks: {pb1: {playback_id: 'pb1', turn_id: 't9', status: 'completed'}},
    jobs: {j1: {job_id: 'j1', turn_id: 't9', status: 'completed', subject: {label: 'Dog Data'}}}});
  await s.client.refresh();
  s.env.state = {...s.env.state, session_id: 's2', revision: 1};
  await s.client.refresh();
  await s.client.refresh();
  assert.deepEqual(s.spoken, []);
  assert.equal(s.players.length, 0);
  assert.equal(s.calls.some(call => ['/api/jarvis/playback', '/api/replies/heard'].includes(call.url)), false);
});

const vectors = JSON.parse(read('test_prefix_vectors.json')).vectors;
const prefixOf = (text, extra = {}) => ({status: 'playing', completed_phrases: [], heard_prefix: {chars: Array.from(text).length, text, alignment: 'provider'}, ...extra});
const until = async check => { for (let i = 0; i < 200 && !check(); i++) await new Promise(resolve => setTimeout(resolve, 1)); };
const sha = text => createHash('sha256').update(text, 'utf8').digest('hex');

test('a heard prefix is sent with explicit code point units and the exact UTF-8 hash, and never text', async () => {
  const s = setup({post: async () => json({})});
  await s.client.refresh();
  s.env.playback = fullPlayback();
  const p = player();
  s.client.trackPlayback(p, {generation: 4, turnId: 't1', style: 'original'});
  p.callback(prefixOf('Café 🐕. '));
  await until(() => playbackPosts(s).some(body => body.op === 'update'));
  const update = playbackPosts(s).find(body => body.op === 'update');
  assert.deepEqual(update.heard_prefix, {chars: 8, sha256: sha('Café 🐕. '), alignment: 'provider'});
  assert.deepEqual(update.completed_phrases, []);
});

test('the shared Unicode vectors hash identically to the Python backend', async () => {
  for (const vector of vectors) {
    const s = setup({post: async () => json({})});
    await s.client.refresh();
    s.env.playback = fullPlayback();
    const p = player();
    s.client.trackPlayback(p, {generation: 4, turnId: 't1', style: 'original'});
    p.callback(prefixOf(vector.prefix));
    await until(() => playbackPosts(s).some(body => body.op === 'update'));
    const update = playbackPosts(s).find(body => body.op === 'update');
    assert.deepEqual(update.heard_prefix, {chars: vector.chars, sha256: vector.sha256, alignment: 'provider'}, vector.name);
  }
});

test('a prefix whose count disagrees with its text or that is not well formed is never sent', async () => {
  for (const bad of [{chars: 9, text: 'Café 🐕. ', alignment: 'provider'}, {chars: 1, text: '\ud83d', alignment: 'provider'},
    {chars: 0, text: '', alignment: 'provider'}, {chars: 3, alignment: 'provider'}]) {
    const s = setup({post: async () => json({})});
    await s.client.refresh();
    s.env.playback = fullPlayback();
    const p = player();
    s.client.trackPlayback(p, {generation: 4, turnId: 't1', style: 'original'});
    p.callback({status: 'playing', completed_phrases: [], heard_prefix: bad});
    await tick(); await tick(); await tick();
    assert.ok(playbackPosts(s).filter(body => body.op === 'update').every(body => !('heard_prefix' in body)), JSON.stringify(bad));
  }
});

test('prefix updates are monotonic, never ride a completed update, and a cancel carries the last proven prefix', async () => {
  const s = setup({post: async () => json({})});
  await s.client.refresh();
  s.env.playback = fullPlayback();
  const p = player();
  s.client.trackPlayback(p, {generation: 4, turnId: 't1', style: 'spoken'});
  p.callback(prefixOf('One. '));
  p.callback(prefixOf('One. '));
  p.callback(prefixOf('One. Two. '));
  p.callback(prefixOf('One. ', {status: 'cancelled'}));
  await until(() => playbackPosts(s).some(body => body.status === 'cancelled'));
  const updates = playbackPosts(s).filter(body => body.op === 'update');
  assert.deepEqual(updates.map(body => [body.status, body.heard_prefix?.chars]), [['playing', 5], ['playing', 10], ['cancelled', undefined]]);
  const done = setup({post: async () => json({})});
  await done.client.refresh();
  done.env.playback = fullPlayback();
  const q = player();
  done.client.trackPlayback(q, {generation: 4, turnId: 't1', style: 'spoken'});
  q.callback(prefixOf('One. ', {status: 'completed'}));
  await tick(); await tick(); await tick();
  const completed = playbackPosts(done).find(body => body.op === 'update');
  assert.equal(completed.status, 'completed');
  assert.equal('heard_prefix' in completed, false);
});

test('a backend that rejects heard_prefix is retried once without it and the ledger still completes', async () => {
  const s = setup({post: async () => json({})});
  await s.client.refresh();
  const base = fullPlayback();
  s.env.playback = call => 'heard_prefix' in call.body ? json({error: 'unknown field'}, 400) : base(call);
  const p = player();
  s.client.trackPlayback(p, {generation: 4, turnId: 't1', style: 'original'});
  p.callback(prefixOf('One. '));
  p.callback(prefixOf('One. Two. '));
  p.callback({status: 'completed', completed_phrases: ['p1']});
  await until(() => playbackPosts(s).some(body => body.status === 'completed'));
  const updates = playbackPosts(s).filter(body => body.op === 'update');
  assert.equal(updates.filter(body => 'heard_prefix' in body).length, 1);
  assert.deepEqual(updates.filter(body => !('heard_prefix' in body)).map(body => body.status), ['playing', 'completed']);
  assert.deepEqual(updates.at(-1).completed_phrases, ['fb1']);
});

test('a stale generation can neither create nor complete a record', async () => {
  const s = setup({post: async () => json({})});
  await s.client.refresh();
  s.env.playback = fullPlayback();
  const stale = player();
  s.client.trackPlayback(stale, {generation: 4, turnId: 't1', style: 'original'});
  s.voice.generation = 5;
  stale.callback({status: 'playing'});
  stale.callback({status: 'completed', completed_phrases: ['p1']});
  await tick(); await tick(); await tick();
  assert.deepEqual(playbackPosts(s), []);
  const live = setup({post: async () => json({})});
  await live.client.refresh();
  live.env.playback = fullPlayback();
  live.voice.generation = 4;
  const q = player();
  live.client.trackPlayback(q, {generation: 4, turnId: 't1', style: 'original'});
  q.callback({status: 'playing'});
  await tick(); await tick();
  live.voice.generation = 5;
  q.callback(prefixOf('One. '));
  q.callback({status: 'completed', completed_phrases: ['p1']});
  await tick(); await tick(); await tick();
  const updates = playbackPosts(live).filter(body => body.op === 'update');
  assert.deepEqual(updates.map(body => body.status), ['playing', 'cancelled']);
  assert.ok(updates.every(body => body.completed_phrases.length === 0 && !('heard_prefix' in body && body.status === 'completed')));
});

test('prepare binds the app record before speech and an unattached binding is cancelled', async () => {
  const s = setup({post: async () => json({})});
  await s.client.refresh();
  s.env.playback = fullPlayback();
  s.voice.generation = 6;
  const bound = await s.client.tracker('t1', 6).prepare({style: 'spoken'});
  assert.deepEqual(plain(bound.fields), {playback_id: 'pb1', session_id: 's1', client_id: s.client.clientId, epoch: 6});
  assert.deepEqual(playbackPosts(s).map(body => body.op), ['create']);
  bound.cancel();
  await tick(); await tick();
  assert.deepEqual(playbackPosts(s).map(body => body.status ?? body.op), ['create', 'cancelled']);
  bound.cancel();
  await tick();
  assert.equal(playbackPosts(s).length, 2);
});

test('prepare returns nothing for a stale generation or a rejected create, so the rendition is never bound', async () => {
  const s = setup({post: async () => json({})});
  await s.client.refresh();
  s.env.playback = fullPlayback();
  s.voice.generation = 7;
  assert.equal(await s.client.tracker('t1', 6).prepare({style: 'spoken'}), null);
  assert.deepEqual(playbackPosts(s), []);
  s.env.playback = () => json({error: 'no'}, 409);
  assert.equal(await s.client.tracker('t1', 7).prepare({style: 'spoken'}), null);
});

test('a bound player that attaches after prepare is the only owner; cancel after attach is a no-op', async () => {
  const s = setup({post: async () => json({})});
  await s.client.refresh();
  s.env.playback = fullPlayback();
  s.voice.generation = 6;
  const bound = await s.client.tracker('t1', 6).prepare({style: 'spoken'});
  const p = player();
  bound.track(p);
  bound.cancel();
  p.callback({status: 'playing'});
  p.callback({status: 'completed'});
  await tick(); await tick(); await tick();
  assert.deepEqual(playbackPosts(s).map(body => body.status ?? body.op), ['create', 'playing', 'completed']);
});

test('an incidental original stays separately visible while canonical submitted or unsent text is spoken', async () => {
  for (const canonical of ['Input submitted to B. Its task is not complete.', 'The exact message is prepared but not sent.']) {
    const s = setup({post: async () => json(receipt('r1', canonical, {available_readback: READBACK}))});
    await s.client.refresh();
    await s.client.submit('Ask it to explain those numbers');
    const record = s.records.get('r1');
    assert.equal(record.reply.target, 'Jarvis');
    assert.equal(record.reply.text, canonical);
    assert.equal(record.availableReply.target, READBACK.target.label);
    assert.equal(record.availableReply.text, READBACK.original);
    assert.equal(record.availableReply.turn_id, undefined);
    assert.deepEqual(s.spoken, [canonical]);
    assert.equal(s.replyCalls[0].spokenReady, true);
  }
});

test('snapshot recovery retains the incidental original without replacing or speaking the canonical reply', async () => {
  const canonical = 'Input submitted to B. Its task is not complete.';
  const s = setup({post: async () => json({})});
  s.env.state = snapshot({revision: 2, requests: {r9: 't9'}, turns: {t9: turn('t9', 'r9', 'completed',
    {result: {text: canonical, available_readback: READBACK}})}});
  await s.client.refresh();
  await s.client.refresh();
  assert.equal(s.records.get('r9').reply.text, canonical);
  assert.equal(s.records.get('r9').availableReply.text, READBACK.original);
  assert.equal(s.replyCalls.length, 0);
  assert.equal(heardPosts(s).length, 0);
  assert.equal(s.calls.filter(call => call.url === '/api/jarvis/playback').length, 0);
});

test('a primary readback already showing the same original does not duplicate its preserved block', async () => {
  const s = setup({post: async () => json(receipt('r1', READBACK.original, {readback: READBACK, available_readback: READBACK}))});
  await s.client.refresh();
  await s.client.submit('Read B');
  assert.equal(s.records.get('r1').reply.text, READBACK.original);
  assert.equal(s.records.get('r1').availableReply, null);
  assert.deepEqual(s.spoken, [READBACK.original]);
});

test('visual reference is a separate payload field and does not replace text, subject or captured recipient', async () => {
  const reference = {workspace_id: 'wa', creation_id: 'report', version_id: 'v1', source: {thread_id: 'unrelated-author'}};
  const s = setup({state: snapshot({subject: {tab: '@b', pane: '%b', identity: 'b:original'}}), post: call => json(receipt(call.body.request_id, 'Saved'))});
  await s.client.submit('Tell B: keep these exact words.', 'typed', {visual_reference: reference});
  assert.deepEqual(s.posts()[0].body.visual_reference, reference);
  assert.equal(s.posts()[0].body.text, 'Tell B: keep these exact words.');
  assert.equal(s.posts()[0].body.capture_target, undefined);
  assert.equal(s.client.snapshot.subject.tab, '@b');
});
