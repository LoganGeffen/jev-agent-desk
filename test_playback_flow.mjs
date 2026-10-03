import assert from 'node:assert/strict';
import {createHash, webcrypto} from 'node:crypto';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const read = name => readFileSync(new URL(name, import.meta.url), 'utf8');
const wait = async (check, label = 'condition') => {
  for (let i = 0; i < 400 && !check(); i++) await new Promise(resolve => setTimeout(resolve, 1));
  assert.ok(check(), 'timed out waiting for ' + label);
};
const settle = () => new Promise(resolve => setTimeout(resolve, 25));
const json = (body, status = 200) => ({ok: status < 400, status, headers: {get: () => 'application/json'}, json: async () => body});
const frame = (text, start, ms, per = 10) => {
  const chars = [...text];
  return {data: JSON.stringify({audio: Buffer.alloc(ms * 48).toString('base64'),
    alignment: {chars, starts: chars.map((_, index) => start + index * per), durations: chars.map(() => per)}})};
};

const REF = {id: 'att-1', tab: '@b', pane: '%b', identity: 'b:original', label: 'B · pane 1'};
const ORIGINAL = 'Café 🐕. Next sentence here. ';
const FIRST = 'Café 🐕. ', SECOND = 'Next sentence here. ';
const TERMINAL = new Set(['completed', 'cancelled', 'failed']);

function pushable(signal) {
  const encoder = new TextEncoder(), chunks = [];
  let waiter = null, closed = false, aborted = false;
  const wake = () => { const resume = waiter; waiter = null; resume?.(); };
  signal?.addEventListener('abort', () => { aborted = true; wake(); });
  const stream = {
    push(event) { chunks.push(encoder.encode(JSON.stringify(event) + '\n')); wake(); },
    close() { closed = true; wake(); },
    response: {ok: true, status: 200, headers: {get: () => 'application/x-ndjson'}, body: {getReader: () => ({
      async read() {
        for (;;) {
          if (aborted) throw Object.assign(new Error('aborted'), {name: 'AbortError'});
          if (chunks.length) return {value: chunks.shift(), done: false};
          if (closed) return {done: true};
          await new Promise(resolve => { waiter = resolve; });
        }
      },
      releaseLock() {},
    })}},
  };
  return stream;
}

function backend({session = 's1', turnId = 't1'} = {}) {
  const s = {playbacks: new Map(), rejected: [], heard: [], direct: [], renders: [], stream: null, createGate: null, nextId: 0, updates: []};
  const bad = (message, status = 409) => { s.rejected.push(message); return json({error: message}, status); };
  const source = playback => playback.generated_text;
  s.playback = body => {
    if (body.session_id !== session) return bad('Stale session');
    if (body.op === 'create') {
      if (body.turn_id !== turnId) return bad('Unknown turn');
      if ([...s.playbacks.values()].some(p => p.client_id === body.client_id && p.epoch > body.epoch)) return bad('Stale epoch');
      for (const prior of s.playbacks.values()) if (!TERMINAL.has(prior.status)) prior.status = 'cancelled';
      const id = 'pb' + ++s.nextId, spoken = body.style === 'spoken';
      const record = {playback_id: id, turn_id: turnId, client_id: body.client_id, epoch: body.epoch, style: body.style, status: 'generated',
        full_boundary_id: 'fb-' + id, completed_phrases: [], generated_text: spoken ? '' : ORIGINAL, rendition_pending: spoken, heard_prefix: null};
      s.playbacks.set(id, record);
      return json({playback_id: id, turn_id: turnId, client_id: record.client_id, epoch: record.epoch, full_boundary_id: record.full_boundary_id, status: 'generated'});
    }
    const record = s.playbacks.get(body.playback_id);
    if (!record || record.client_id !== body.client_id || record.epoch !== body.epoch) return bad('Unknown or stale owner');
    if (TERMINAL.has(record.status)) return bad('Playback already ended');
    if (!['playing', 'completed', 'cancelled', 'failed'].includes(body.status)) return bad('Invalid status');
    const phrases = body.completed_phrases;
    if (body.status === 'completed' ? JSON.stringify(phrases) !== JSON.stringify([record.full_boundary_id]) : phrases.length) return bad('Boundary mismatch');
    if (body.status === 'completed' && record.rendition_pending) return bad('Spoken rendition is not yet app-confirmed');
    if ('heard_prefix' in body) {
      const prefix = body.heard_prefix;
      if (Object.keys(prefix).sort().join() !== 'alignment,chars,sha256' || prefix.alignment !== 'provider') return bad('Invalid provider-aligned prefix');
      const points = Array.from(source(record));
      if (!Number.isInteger(prefix.chars) || prefix.chars < (record.heard_prefix?.chars ?? 0) || prefix.chars > points.length) return bad('Heard prefix is out of range or regresses');
      if (prefix.sha256 !== createHash('sha256').update(points.slice(0, prefix.chars).join(''), 'utf8').digest('hex')) return bad('Heard prefix does not match exact app source');
      record.heard_prefix = prefix;
    }
    record.status = body.status;
    record.completed_phrases = phrases;
    s.updates.push({playback_id: record.playback_id, status: body.status, chars: body.heard_prefix?.chars ?? null});
    if (record.status === 'completed') s.heard.push(REF);
    return json({playback_id: record.playback_id, status: record.status});
  };
  s.render = (body, signal) => {
    s.renders.push(body);
    const owner = ['playback_id', 'session_id', 'client_id', 'epoch'];
    if (owner.some(key => key in body)) {
      const record = s.playbacks.get(body.playback_id);
      if (!owner.every(key => key in body) || body.session_id !== session || !record || record.client_id !== body.client_id
        || record.epoch !== body.epoch || TERMINAL.has(record.status) || record.style !== 'spoken') return json({error: 'Tracked rendition owner is stale'}, 400);
      s.bound = record;
    } else s.bound = null;
    const stream = pushable(signal);
    const record = s.bound;
    stream.delta = text => { if (record) record.generated_text += text; stream.push({type: 'text', text}); };
    stream.finish = text => {
      if (record) { assert.equal(record.generated_text, text); record.rendition_pending = false; }
      stream.push({type: 'result', result: {text, mode: 'spoken'}}); stream.close();
    };
    s.stream = stream;
    return stream.response;
  };
  return s;
}

async function desk({style = 'original', server = backend()} = {}) {
  const elements = new Map(), listeners = {}, sockets = [], sources = [];
  const document = {hidden: false,
    getElementById(id) { if (!elements.has(id)) elements.set(id, {setAttribute(name, value) { this[name] = value; }}); return elements.get(id); },
    addEventListener(name, callback) { listeners[name] = callback; }};
  class Context {
    currentTime = 0; sampleRate = 16000; destination = {};
    audioWorklet = {addModule: async () => {}};
    resume = async () => {}; close = async () => {};
    createMediaStreamSource = () => ({connect() {}, disconnect() {}});
    createBuffer = (_, length, rate) => ({duration: length / rate, getChannelData: () => new Float32Array(length)});
    createBufferSource() {
      const source = {connect() {}, disconnect() {}, start() { this.started = true; }, stop() { this.stopped = true; }};
      sources.push(source); return source;
    }
  }
  class Socket {
    static OPEN = 1;
    readyState = 1; bufferedAmount = 0;
    constructor(url) { this.url = url; this.messages = []; sockets.push(this); }
    send(text) { this.messages.push(JSON.parse(text)); }
    close() { this.closed = true; }
  }
  const direct = [], calls = [];
  const fetch = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : undefined;
    calls.push({url, body});
    if (url === '/api/jarvis/state') return json({session_id: 's1', revision: 1, subject: null, turns: {}, requests: {}, drafts: {}, actions: {}, jobs: {}, playbacks: {}, clarifications: {}});
    if (url === '/api/jarvis/turn') return json({session_id: 's1', turn_id: 't1', request_id: body.request_id, revision: 2, status: 'completed', text: 'Reading B.', actions: [],
      readback: {original: ORIGINAL, target: REF, reply_ref: REF}});
    if (url === '/api/jarvis/playback') {
      if (body.op === 'create' && server.createGate) await server.createGate;
      return server.playback(body);
    }
    if (url === '/api/spoken-version') return server.render(body, options.signal);
    if (url === '/api/replies/heard') { direct.push(body); return json({}); }
    if (url === '/api/voice-event') return json({});
    throw new Error('unexpected ' + url);
  };
  const context = vm.createContext({document, window: {addEventListener() {}},
    navigator: {mediaDevices: {getUserMedia: async () => ({getTracks: () => []})}}, AudioContext: Context, WebSocket: Socket,
    AudioWorkletNode: class { port = {}; connect() {} disconnect() {} }, AbortController, setTimeout, clearTimeout, Date, Uint8Array, DataView,
    TextDecoder, TextEncoder, btoa, atob, fetch, crypto: webcrypto});
  vm.runInContext(read('readback.js') + read('voice.js') + read('jarvis.js') + '\nglobalThis.Controls = VoiceControls; globalThis.JarvisClient = JarvisClient;', context);
  const voice = new context.Controls(async () => {});
  voice.configure({configured: true, websocket_url: 'ws://test'});
  document.getElementById('speech-style').value = style;
  await voice.start();
  voice.capture.ws.onmessage({data: JSON.stringify({message_type: 'session_started'})});
  let ids = 0;
  const client = new context.JarvisClient({fetch, newId: () => 'r' + ++ids, now: () => Date.now(), deadlineMs: 5000, lostGraceMs: 500, retain() {}, onChange() {}});
  client.voice = voice;
  await client.refresh();
  const readback = () => sockets.at(-1);
  return {server, voice, client, elements, listeners, document, sockets, sources, readback, direct, calls,
    submit: () => client.submit('read B', 'voice'),
    updates: () => server.updates.map(update => [update.playback_id, update.status, update.chars]),
    statuses: () => [...server.playbacks.values()].map(record => [record.playback_id, record.status])};
}

test('original wording: heard prefixes advance with ended audio and a full completion acknowledges the exact reply once', async () => {
  const d = await desk();
  await d.submit();
  await wait(() => d.sockets.length === 2, 'readback socket');
  assert.deepEqual(d.readback().messages.map(message => message.text), [FIRST, SECOND, '']);
  d.readback().onmessage(frame(FIRST, 0, 200));
  d.readback().onmessage(frame(SECOND, 200, 300));
  await wait(() => d.updates().some(update => update[1] === 'playing'), 'playing');
  assert.deepEqual(d.server.updates.filter(update => update.chars !== null), []);
  d.sources[0].onended();
  await wait(() => d.server.updates.some(update => update.chars === 8), 'first prefix');
  d.readback().onmessage({data: '{"isFinal":true}'});
  d.sources[1].onended();
  await wait(() => d.server.heard.length === 1, 'full ack');
  assert.deepEqual(d.server.heard, [REF]);
  assert.deepEqual(d.statuses(), [['pb1', 'completed']]);
  assert.deepEqual(d.server.rejected, []);
  assert.deepEqual(d.direct, []);
  const record = d.server.playbacks.get('pb1');
  assert.deepEqual(record.completed_phrases, [record.full_boundary_id]);
  assert.equal(d.server.updates.at(-1).chars, null);
});

test('original wording: stopping after one heard phrase records only that prefix and never marks the reply heard', async () => {
  const d = await desk();
  await d.submit();
  await wait(() => d.sockets.length === 2, 'readback socket');
  d.readback().onmessage(frame(FIRST, 0, 200));
  d.readback().onmessage(frame(SECOND, 200, 300));
  d.sources[0].onended();
  await wait(() => d.server.updates.some(update => update.chars === 8), 'prefix');
  d.voice.stopReadback('button');
  await wait(() => d.statuses()[0][1] === 'cancelled', 'cancel');
  assert.deepEqual(d.updates().at(-1), ['pb1', 'cancelled', null]);
  assert.equal(d.server.playbacks.get('pb1').heard_prefix.chars, 8);
  assert.deepEqual(d.server.heard, []);
  assert.ok(d.sources[1].stopped);
  const count = d.sources.length, posts = d.calls.length;
  d.readback().onmessage(frame(SECOND, 500, 300));
  d.readback().onmessage({data: '{"isFinal":true}'});
  d.sources[1].onended?.();
  await settle();
  assert.equal(d.sources.length, count);
  assert.equal(d.calls.length, posts);
  assert.deepEqual(d.server.heard, []);
  assert.deepEqual(d.server.rejected, []);
});

test('spoken rendition: the app binds its playback before streaming and an interrupt before the cache final keeps the rendered prefix', async () => {
  const d = await desk({style: 'spoken'});
  const submitted = d.submit();
  await wait(() => d.server.renders.length === 1, 'bound render request');
  assert.deepEqual(d.calls.filter(call => call.url === '/api/jarvis/playback').map(call => call.body.op), ['create']);
  const request = d.server.renders[0];
  assert.deepEqual({playback_id: request.playback_id, session_id: request.session_id, client_id: request.client_id, epoch: request.epoch},
    {playback_id: 'pb1', session_id: 's1', client_id: d.client.clientId, epoch: d.voice.generation});
  assert.equal(request.stream, true);
  assert.equal(request.text, ORIGINAL);
  await submitted;
  const rendered = 'Rendered café 🐕. ', more = 'More words follow. ';
  d.server.stream.delta(rendered);
  await wait(() => d.sockets.length === 2, 'streaming player created on first delta');
  assert.deepEqual(d.readback().messages.map(message => message.text), [rendered]);
  d.server.stream.delta(more);
  await wait(() => d.readback().messages.length === 2, 'second phrase');
  d.readback().onmessage(frame(rendered, 0, 300));
  d.readback().onmessage(frame(more, 300, 300));
  d.sources[0].onended();
  await wait(() => d.server.updates.some(update => update.chars === Array.from(rendered).length), 'rendered prefix');
  d.voice.stopReadback('speech onset');
  await wait(() => d.statuses()[0][1] === 'cancelled', 'cancel');
  const record = d.server.playbacks.get('pb1');
  assert.equal(record.generated_text, rendered + more);
  assert.equal(record.rendition_pending, true);
  assert.equal(record.heard_prefix.chars, Array.from(rendered).length);
  assert.deepEqual(d.server.heard, []);
  assert.deepEqual(d.server.rejected, []);
  assert.deepEqual(d.direct, []);
  assert.equal(d.calls.filter(call => call.url === '/api/jarvis/playback' && call.body.status === 'completed').length, 0);
});

test('spoken rendition: a stream that finishes and plays fully acknowledges only after the cache final and complete audio', async () => {
  const d = await desk({style: 'spoken'});
  const submitted = d.submit();
  await wait(() => d.server.renders.length === 1, 'render');
  await submitted;
  const rendered = 'Rendered café 🐕. ', tail = 'Tail words. ';
  d.server.stream.delta(rendered);
  await wait(() => d.sockets.length === 2, 'player');
  d.readback().onmessage(frame(rendered, 0, 300));
  d.server.stream.delta(tail);
  await wait(() => d.readback().messages.length === 2, 'tail phrase sent');
  d.readback().onmessage(frame(tail, 300, 200));
  d.sources[0].onended();
  await wait(() => d.server.updates.some(update => update.chars === Array.from(rendered).length), 'prefix');
  d.server.stream.finish(rendered + tail);
  await wait(() => d.elements.get('reply-spoken').textContent === rendered + tail, 'result shown');
  d.readback().onmessage({data: '{"isFinal":true}'});
  assert.deepEqual(d.server.heard, []);
  d.sources[1].onended();
  await wait(() => d.server.heard.length === 1, 'ack');
  assert.deepEqual(d.statuses(), [['pb1', 'completed']]);
  assert.deepEqual(d.server.rejected, []);
});

test('spoken rendition: stopping before first audio cancels without a prefix and late audio cannot restart or complete', async () => {
  const d = await desk({style: 'spoken'});
  const submitted = d.submit();
  await wait(() => d.server.renders.length === 1, 'render');
  await submitted;
  d.server.stream.delta('Rendered café 🐕. ');
  await wait(() => d.sockets.length === 2, 'player');
  d.voice.stopReadback('button');
  await wait(() => d.statuses()[0]?.[1] === 'cancelled', 'cancel');
  assert.deepEqual(d.updates(), [['pb1', 'cancelled', null]]);
  d.readback().onmessage(frame('Rendered café 🐕. ', 0, 300));
  d.readback().onmessage({data: '{"isFinal":true}'});
  d.server.stream.finish?.('Rendered café 🐕. ');
  await settle();
  assert.equal(d.sources.length, 0);
  assert.deepEqual(d.updates(), [['pb1', 'cancelled', null]]);
  assert.deepEqual(d.server.heard, []);
});

test('spoken rendition: a stop while the app record is still being created never opens the render stream and ends the record cancelled', async () => {
  const server = backend();
  let release;
  server.createGate = new Promise(resolve => { release = resolve; });
  const d = await desk({style: 'spoken', server});
  d.submit();
  await wait(() => d.calls.some(call => call.url === '/api/jarvis/playback'), 'create in flight');
  d.voice.stopReadback('button');
  release();
  await wait(() => d.statuses()[0]?.[1] === 'cancelled', 'cancelled record');
  await settle();
  assert.deepEqual(server.renders, []);
  assert.deepEqual(d.updates(), [['pb1', 'cancelled', null]]);
  assert.deepEqual(server.heard, []);
});

test('replay opens a newer epoch and the older owner can neither complete its record nor resurrect audio', async () => {
  const d = await desk();
  await d.submit();
  await wait(() => d.sockets.length === 2, 'first player');
  const first = d.readback();
  d.readback().onmessage(frame(FIRST, 0, 200));
  d.readback().onmessage(frame(SECOND, 200, 300));
  d.sources[0].onended();
  await wait(() => d.server.updates.some(update => update.chars === 8), 'first prefix');
  const stale = d.voice.playing.onProgress;
  d.elements.get('replay-reply').onclick();
  await wait(() => d.sockets.length === 3, 'replay player');
  await wait(() => d.statuses()[0][1] === 'cancelled', 'old record cancelled');
  assert.deepEqual(d.statuses(), [['pb1', 'cancelled']]);
  const posts = d.calls.length;
  stale({status: 'completed', completed_phrases: ['p1', 'p2']});
  first.onmessage(frame(SECOND, 500, 300));
  first.onmessage({data: '{"isFinal":true}'});
  await settle();
  assert.equal(d.calls.length, posts);
  assert.deepEqual(d.server.heard, []);
  const replay = d.readback();
  replay.onmessage(frame(FIRST, 0, 200));
  replay.onmessage(frame(SECOND, 200, 300));
  replay.onmessage({data: '{"isFinal":true}'});
  await wait(() => d.statuses().length === 2, 'newer epoch record');
  assert.ok(d.server.playbacks.get('pb2').epoch > d.server.playbacks.get('pb1').epoch);
  d.sources.slice(2).forEach(source => source.onended());
  await wait(() => d.server.heard.length === 1, 'replay ack');
  assert.deepEqual(d.statuses(), [['pb1', 'cancelled'], ['pb2', 'completed']]);
  assert.deepEqual(d.server.rejected, []);
  assert.deepEqual(d.direct, []);
});

test('a hidden page stops audible readback and records a cancelled playback that cannot be completed later', async () => {
  const d = await desk();
  await d.submit();
  await wait(() => d.sockets.length === 2, 'player');
  d.readback().onmessage(frame(FIRST, 0, 200));
  d.readback().onmessage(frame(SECOND, 200, 300));
  d.sources[0].onended();
  await wait(() => d.server.updates.some(update => update.chars === 8), 'prefix');
  d.document.hidden = true;
  d.listeners.visibilitychange();
  await wait(() => d.statuses()[0][1] === 'cancelled', 'cancelled');
  d.readback().onmessage({data: '{"isFinal":true}'});
  d.sources[1].onended?.();
  await settle();
  assert.deepEqual(d.statuses(), [['pb1', 'cancelled']]);
  assert.deepEqual(d.server.heard, []);
  assert.deepEqual(d.direct, []);
  assert.deepEqual(d.server.rejected, []);
});

test('late first audio after a newer generation began cannot create or complete an older record', async () => {
  const d = await desk();
  await d.submit();
  await wait(() => d.sockets.length === 2, 'player');
  d.voice.stopReadback('button');
  await wait(() => d.statuses()[0]?.[1] === 'cancelled' || d.server.playbacks.size === 0, 'settled');
  const posts = d.calls.length;
  d.readback().onmessage(frame(FIRST, 0, 200));
  d.readback().onmessage({data: '{"isFinal":true}'});
  await settle();
  assert.equal(d.sources.length, 0);
  assert.equal(d.calls.length, posts);
  assert.deepEqual(d.server.heard, []);
  assert.ok(d.server.playbacks.size <= 1);
});
