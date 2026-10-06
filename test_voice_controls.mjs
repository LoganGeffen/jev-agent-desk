import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function fakeClock() {
  let now = 1_000_000, seq = 0;
  const timers = new Map();
  const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
  const clock = {
    setTimeout(callback, ms = 0) { const id = ++seq; timers.set(id, {id, callback, at: now + ms}); return id; },
    clearTimeout(id) { timers.delete(id); },
    Date: class extends Date { static now() { return now; } },
    pending: () => timers.size,
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = [...timers.values()].filter(timer => timer.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
        if (!due) break;
        timers.delete(due.id);
        now = Math.max(now, due.at);
        due.callback();
        await flush();
      }
      now = end;
      await flush();
    },
  };
  return clock;
}

function setup({clock = null, automatic = false} = {}) {
  const elements = new Map();
  const listeners = {};
  const logs = [];
  const submitted = [];
  const submissions = [];
  const sources = [];
  const sockets = [];
  const tracks = [{stopped: false, stop() { this.stopped = true; }}];
  const stream = {getTracks: () => tracks};
  class Context {
    currentTime = 0;
    state = 'running';
    sampleRate = 16000;
    createBuffer = (_, length, rate) => ({duration: length / rate, getChannelData: () => new Float32Array(length)});
    destination = {};
    audioWorklet = {addModule: async () => {}};
    resume = async () => {};
    close = async () => { this.closed = true; };
    createMediaStreamSource = () => ({connect() {}, disconnect() {}});
    decodeAudioData = async () => ({});
    createBufferSource() {
      const source = {connect() {}, disconnect() {}, start() { this.started = true; }, stop() { this.stopped = true; }};
      sources.push(source);
      return source;
    }
  }
  class Socket {
    constructor(url) { this.url = url; this.messages = []; sockets.push(this); }
    static OPEN = 1;
    readyState = 1;
    bufferedAmount = 0;
    send(text) { this.messages.push(JSON.parse(text)); }
    close() { this.closed = true; }
  }
  const document = {
    hidden: false,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {setAttribute(name, value) { this[name] = value; }});
      return elements.get(id);
    },
    addEventListener(name, callback) { listeners[name] = callback; }
  };
  const context = vm.createContext({document, window: {addEventListener() {}},
    navigator: {mediaDevices: {getUserMedia: async () => stream}},
    AudioContext: Context, WebSocket: Socket,
    AudioWorkletNode: class { port = {}; connect() {} disconnect() {} },
    AbortController, setTimeout: clock?.setTimeout ?? setTimeout, clearTimeout: clock?.clearTimeout ?? clearTimeout,
    ...(clock ? {Date: clock.Date} : {}), Uint8Array, DataView, TextDecoder, btoa, atob,
    fetch: async (url, options) => {
      if (url === '/api/voice-event') logs.push(JSON.parse(options.body));
      return {ok: true, arrayBuffer: async () => new ArrayBuffer(8)};
    }
  });
  vm.runInContext(readFileSync(new URL('./readback.js', import.meta.url), 'utf8'), context);
  vm.runInContext(readFileSync(new URL('./voice.js', import.meta.url), 'utf8') + '\nglobalThis.Controls = VoiceControls;', context);
  const voice = new context.Controls(async (text, target, source, meta) => {
    submitted.push(text);
    submissions.push(JSON.parse(JSON.stringify({text, target, source, meta})));
  });
  voice.localWake = false;
  voice.autoAttempted = !automatic;
  voice.configure({configured: true, websocket_url: 'ws://test'});
  return {voice, context, document, elements, listeners, logs, submitted, submissions, sources, tracks, stream, sockets};
}

async function listening(s) {
  await s.voice.start();
  s.voice.capture.ws.onmessage({data: JSON.stringify({message_type: 'session_started'})});
}

test('voice toggle state preserves icon content', async () => {
  const s = setup();
  const button = s.elements.get('voice-toggle');
  button.textContent = 'icon';
  await listening(s);
  assert.equal(button.textContent, 'icon');
  assert.equal(button['aria-pressed'], 'true');
  s.voice.stop();
  assert.equal(button.textContent, 'icon');
  assert.equal(button['aria-pressed'], 'false');
});

test('stop cancels audible readback without submitting an agent action', async () => {
  const s = setup();
  await listening(s);
  await s.voice.speak('hello', s.voice.generation);
  assert.equal(s.sources[0].started, true);
  s.voice.stopReadback('button');
  assert.equal(s.sources[0].stopped, true);
  assert.equal(s.voice.playing, null);
  assert.deepEqual(s.submitted, []);
  s.voice.stop();
});

test('a late speech response cannot play after interruption', async () => {
  const s = setup();
  await listening(s);
  let finish;
  const original = s.context.fetch;
  s.context.fetch = (url, options) => url === '/api/speech'
    ? new Promise(resolve => { finish = resolve; }) : original(url, options);
  const speaking = s.voice.speak('old reply', s.voice.generation);
  const pending = s.voice.pending;
  s.voice.stopReadback('microphone speech onset');
  assert.equal(pending.signal.aborted, true);
  finish({ok: true, arrayBuffer: async () => new ArrayBuffer(8)});
  await speaking;
  assert.equal(s.sources.length, 0);
  s.voice.stop();
});

test('microphone onset stops speech before a transcript; only final transcripts submit', async () => {
  const s = setup();
  s.document.getElementById('talk-to-interrupt').checked = true;
  await listening(s);
  await s.voice.speak('reply', s.voice.generation);
  const packet = {data: {pcm: new Int16Array(2048), rms: 0.1}};
  s.voice.capture.processor.port.onmessage(packet);
  s.voice.capture.processor.port.onmessage(packet);
  assert.equal(s.sources[0].stopped, true);
  const receive = message => s.voice.capture.ws.onmessage({data: JSON.stringify(message)});
  receive({message_type: 'partial_transcript', text: 'Go to Alpha, actually'});
  assert.deepEqual(s.submitted, []);
  receive({message_type: 'committed_transcript', text: 'Go to Alpha, actually Beta.'});
  assert.deepEqual(s.submitted, []);
  await s.voice.finishDraft();
  assert.deepEqual(s.submitted, ['Go to Alpha, actually Beta.']);
  assert.equal(s.elements.get('transcript').textContent, '');
  assert.equal(s.elements.has('request'), false);
  s.voice.stop();
});

test('disabling while permission is pending releases the eventual microphone', async () => {
  const s = setup();
  let grant;
  s.context.navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { grant = resolve; });
  const starting = s.voice.start();
  await new Promise(resolve => setImmediate(resolve));
  s.voice.stop();
  grant(s.stream);
  await starting;
  assert.equal(s.tracks[0].stopped, true);
  assert.equal(s.voice.capture, null);
});

test('hesitation and restarted partials never send; the committed correction sends once', async () => {
  const s = setup();
  await listening(s);
  const receive = (message_type, text) => s.voice.capture.ws.onmessage({data: JSON.stringify({message_type, text})});
  receive('partial_transcript', 'Tell Luna, um...');
  await new Promise(resolve => setTimeout(resolve, 1400));
  assert.deepEqual(s.submitted, []);
  receive('partial_transcript', 'Tell Luna, um... Hey, can you...');
  receive('partial_transcript', 'Tell Luna, um... Hey, can you... actually, no, tell Nova:');
  assert.deepEqual(s.submitted, []);
  const final = 'Tell Luna, um... Hey, can you... actually, no, tell Nova: Please explain the test failure.';
  receive('committed_transcript', final);
  await s.voice.finishDraft();
  assert.deepEqual(s.submitted, [final]);
  s.voice.stop();
});

test('hiding the page releases microphone and speech connection', async () => {
  const s = setup();
  await listening(s);
  const capture = s.voice.capture;
  s.document.hidden = true;
  s.listeners.visibilitychange();
  assert.equal(s.tracks[0].stopped, true);
  assert.equal(capture.ws.closed, true);
  assert.equal(capture.context.closed, true);
  assert.equal(s.voice.capture, null);
});

test('permission denial is visible and leaves voice disabled', async () => {
  const s = setup();
  s.context.navigator.mediaDevices.getUserMedia = async () => { throw Object.assign(new Error('denied'), {name: 'NotAllowedError'}); };
  await s.voice.start();
  assert.equal(s.voice.capture, null);
  assert.equal(s.elements.get('voice-status').textContent, 'Microphone permission denied');
});


test('thinking pause joins only unsubmitted segments and binds the original target', async () => {
  const s = setup();
  let target = {pane: '%1', identity: 'one'};
  s.voice.target = () => target;
  await listening(s);
  s.voice.committed('Please fix the parser');
  s.voice.speechOnset();
  s.voice.committed('and preserve comments.');
  target = {pane: '%2', identity: 'two'};
  await s.voice.finishDraft();
  assert.deepEqual(s.submitted, []);
  assert.equal(s.voice.draft.text, 'Please fix the parser and preserve comments.');
  target = {pane: '%1', identity: 'one'};
  await s.voice.finishDraft();
  assert.deepEqual(s.submitted, ['Please fix the parser and preserve comments.']);
  s.voice.committed('What is Nova doing?');
  s.voice.holdDraft();
  assert.equal(s.voice.draft.text, 'What is Nova doing?');
  s.voice.stop();
});

test('resumed speech cancels automatic send while final transcription is pending', async () => {
  const s = setup();
  s.voice.graceMs = 20;
  await listening(s);
  s.voice.committed('First part');
  s.voice.speechOnset();
  await new Promise(resolve => setTimeout(resolve, 40));
  assert.deepEqual(s.submitted, []);
  s.voice.committed('second part');
  await new Promise(resolve => setTimeout(resolve, 40));
  assert.deepEqual(s.submitted, ['First part second part']);
  s.voice.stop();
});

test('playback context is separate and both contexts close on stop', async () => {
  const s = setup();
  await listening(s);
  const capture = s.voice.capture;
  assert.notEqual(capture.context, capture.playback);
  await s.voice.speak('reply', s.voice.generation);
  s.voice.stop();
  assert.equal(capture.context.closed, true);
  assert.equal(capture.playback.closed, true);
});


test('pane identity pins at audio onset before the first partial arrives', async () => {
  const s = setup();
  let target = {pane: '%1', identity: 'original'};
  s.voice.target = () => target;
  await listening(s);
  s.voice.speechOnset();
  target = {pane: '%1', identity: 'replacement'};
  s.voice.capture.ws.onmessage({data: JSON.stringify({message_type: 'partial_transcript', text: 'hello'})});
  s.voice.committed('hello');
  await s.voice.finishDraft();
  assert.deepEqual(s.submitted, []);
  assert.equal(s.voice.draft.target.identity, 'original');
  s.voice.stop();
});

test('page hide retains unfinalized words for explicit review without sending', async () => {
  const s = setup();
  await listening(s);
  s.voice.committed('First sentence.');
  s.voice.capture.ws.onmessage({data: JSON.stringify({message_type: 'partial_transcript', text: 'Still thinking'})});
  s.voice.stop();
  assert.equal(s.voice.draft.text, 'First sentence. Still thinking');
  assert.equal(s.voice.draft.unfinalized, true);
  assert.equal(s.voice.draft.held, true);
  assert.deepEqual(s.submitted, []);
});

test('original-mode new reply clears previous spoken rendition', async () => {
  const s = setup();
  await listening(s);
  s.document.getElementById('speech-style').value = 'original';
  s.document.getElementById('reply-spoken').textContent = 'old rendition';
  await s.voice.speakReply('new original', s.voice.generation);
  assert.equal(s.document.getElementById('reply-original').textContent, 'new original');
  assert.equal(s.document.getElementById('reply-spoken').textContent, '');
  s.voice.stop();
});

test('replaying the same reply reuses rendition and streams audio without resubmitting', async () => {
  const s = setup();
  await listening(s);
  const originalFetch = s.context.fetch;
  const calls = [];
  s.context.fetch = async (url, options) => {
    if (url !== '/api/voice-event') calls.push(url);
    if (url === '/api/spoken-version') return {ok: true, json: async () => ({text: 'Spoken reply.'})};
    return originalFetch(url, options);
  };
  await s.voice.speakReply('Original reply.', s.voice.generation);
  s.voice.stopReadback('interrupted');
  await s.voice.speak('Your words are saved.', s.voice.generation);
  await s.voice.speakReply('Original reply.', s.voice.stopReadback('replay'));
  assert.deepEqual(calls, ['/api/spoken-version', '/api/speech']);
  assert.equal(s.sockets.filter(socket => socket.url.includes('tts=')).length, 2);
  assert.equal(s.elements.get('reply-original').textContent, 'Original reply.');
  assert.equal(s.elements.get('reply-spoken').textContent, 'Spoken reply.');
  assert.deepEqual(s.submitted, []);
  s.voice.stop();
});

test('audio cache separates voice profiles and keeps only three clips', async () => {
  const s = setup();
  await listening(s);
  let requests = 0;
  const originalFetch = s.context.fetch;
  s.context.fetch = async (url, options) => {
    if (url === '/api/speech') requests++;
    return originalFetch(url, options);
  };
  for (const profile of ['current', 'alternate', 'current']) {
    s.document.getElementById('voice-profile').value = profile;
    await s.voice.speak('Same text', s.voice.stopReadback('profile'));
  }
  assert.equal(requests, 2);
  for (const text of ['second', 'third', 'fourth']) {
    await s.voice.speak(text, s.voice.stopReadback('next'));
  }
  assert.equal(s.voice.speechCache.size, 3);
  s.voice.stop();
});

test('a changed reply cannot reuse a previous rendition', async () => {
  const s = setup();
  await listening(s);
  const originalFetch = s.context.fetch;
  let rewrites = 0;
  s.context.fetch = async (url, options) => {
    if (url === '/api/spoken-version') {
      rewrites++;
      return {ok: true, json: async () => ({text: 'Spoken: ' + JSON.parse(options.body).text})};
    }
    return originalFetch(url, options);
  };
  await s.voice.speakReply('First', s.voice.generation);
  await s.voice.speakReply('Second', s.voice.stopReadback('next'));
  assert.equal(rewrites, 2);
  assert.equal(s.elements.get('reply-original').textContent, 'Second');
  assert.equal(s.elements.get('reply-spoken').textContent, 'Spoken: Second');
  s.voice.stop();
});

test('speaker playback sends silence and ignores echo until Stop and listen', async () => {
  const s = setup();
  await listening(s);
  await s.voice.speak('A reply', s.voice.generation);
  const capture = s.voice.capture;
  const chunks = [];
  capture.ws.send = raw => chunks.push(JSON.parse(raw));
  const pcm = new Int16Array(2048).fill(1000);
  const packet = {data: {pcm, rms: 0.1}};
  capture.processor.port.onmessage(packet);
  capture.processor.port.onmessage(packet);
  assert.equal(s.sources[0].stopped, undefined);
  assert.equal(Buffer.from(chunks[0].audio_base_64, 'base64').every(byte => byte === 0), true);
  for (const message_type of ['partial_transcript', 'committed_transcript']) {
    capture.ws.onmessage({data: JSON.stringify({message_type, text: 'A reply'})});
  }
  assert.equal(s.voice.draft, null);
  assert.deepEqual(s.submitted, []);
  s.elements.get('stop-readback').onclick();
  assert.equal(s.sources[0].stopped, true);
  assert.equal(s.voice.recognitionPaused(), true);
  await new Promise(resolve => setTimeout(resolve, 320));
  capture.processor.port.onmessage(packet);
  assert.equal(Buffer.from(chunks.at(-1).audio_base_64, 'base64').some(byte => byte !== 0), true);
  capture.ws.onmessage({data: JSON.stringify({message_type: 'committed_transcript', text: 'My next request'})});
  await s.voice.finishDraft();
  assert.deepEqual(s.submitted, ['My next request']);
  s.voice.stop();
});

test('recognition resumes after speaker playback ends and provider errors stay visible', async () => {
  const s = setup();
  await listening(s);
  await s.voice.speak('A reply', s.voice.generation);
  s.sources[0].onended();
  assert.equal(s.elements.get('stop-readback').hidden, true);
  assert.equal(s.voice.recognitionPaused(), true);
  await new Promise(resolve => setTimeout(resolve, 320));
  assert.equal(s.voice.recognitionPaused(), false);
  await s.voice.speak('Another reply', s.voice.generation);
  s.voice.capture.ws.onmessage({data: JSON.stringify({message_type: 'error', error: 'Provider disconnected'})});
  assert.equal(s.voice.capture, null);
  assert.equal(s.elements.get('voice-status').textContent, 'Provider disconnected');
});

test('identity resolution holds automatic delivery; manual Send accepts the reviewed current session', async () => {
  const s = setup();
  let target = {tab: '@6', pane: '%6', identity: 'process:codex:'};
  s.voice.target = () => target;
  await listening(s);
  s.voice.speechOnset();
  target = {tab: '@6', pane: '%6', identity: 'process:codex:new-conversation'};
  s.voice.committed('Please check my standup tasks.');
  assert.equal(s.voice.draft.held, true);
  assert.match(s.elements.get('speech-draft-status').textContent, /Session changed/);
  await s.voice.finishDraft();
  assert.deepEqual(s.submitted, []);
  await s.voice.finishDraft(true);
  assert.deepEqual(s.submitted, ['Please check my standup tasks.']);
  s.voice.stop();
});

test('noise without recognized words becomes reviewable; late words never auto-send', async () => {
  const s = setup();
  s.voice.recognitionWaitMs = 20;
  s.voice.graceMs = 25;
  await listening(s);
  s.voice.committed('Known words');
  s.voice.speechOnset();
  await new Promise(resolve => setTimeout(resolve, 40));
  assert.equal(s.voice.draft.held, true);
  assert.equal(s.voice.draft.awaitingCommit, false);
  assert.deepEqual(s.submitted, []);
  await s.voice.finishDraft();
  const receive = (message_type, text) => s.voice.capture.ws.onmessage({data: JSON.stringify({message_type, text})});
  receive('partial_transcript', 'Late words');
  receive('committed_transcript', 'Late words');
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.deepEqual(s.submitted, ['Known words']);
  assert.equal(s.voice.draft.held, true);
  assert.equal(s.voice.draft.text, 'Late words');
  s.voice.stop();
});

test('restoring a failed request never overwrites a newer draft', async () => {
  const s = setup();
  assert.equal(s.voice.restoreDraft('First retained request', {pane: '%1'}), true);
  assert.equal(s.voice.draft.held, true);
  assert.equal(s.voice.restoreDraft('Second request', {pane: '%2'}), false);
  assert.equal(s.voice.draft.text, 'First retained request');
  assert.deepEqual(s.submitted, []);
});

test('speaker preparation is visibly not listening and Stop restores recognition', async () => {
  const s = setup();
  await listening(s);
  let finish;
  const originalFetch = s.context.fetch;
  s.context.fetch = (url, options) => url === '/api/speech'
    ? new Promise(resolve => { finish = resolve; }) : originalFetch(url, options);
  const speaking = s.voice.speak('Reply', s.voice.generation);
  assert.equal(!!s.voice.recognitionPaused(), true);
  assert.match(s.elements.get('playback-status').textContent, /Not listening/);
  const capture = s.voice.capture;
  const packet = {data: {pcm: new Int16Array(2048), rms: 0.1}};
  capture.processor.port.onmessage(packet);
  capture.processor.port.onmessage(packet);
  assert.equal(s.voice.pending.signal.aborted, false);
  s.elements.get('stop-readback').onclick();
  assert.equal(s.voice.recognitionPaused(), false);
  finish({ok: true, arrayBuffer: async () => new ArrayBuffer(8)});
  await speaking;
  assert.equal(s.sources.length, 0);
  s.voice.stop();
});

test('viewing a reply with the microphone off preserves original without calling providers', async () => {
  const s = setup();
  const calls = [];
  s.context.fetch = async url => { calls.push(url); throw new Error('No provider expected'); };
  await s.voice.speakReply('Exact original reply.', s.voice.generation);
  assert.equal(s.elements.get('reply-original').textContent, 'Exact original reply.');
  assert.equal(s.elements.get('reply-presentation').open, true);
  assert.deepEqual(calls.filter(url => url !== '/api/voice-event'), []);
});

test('failed readback releases preparation without fallback speech', async () => {
  const s = setup();
  await listening(s);
  const calls = [];
  s.context.fetch = async url => {
    if (url === '/api/voice-event') return {};
    calls.push(url);
    return {ok: false, json: async () => ({error: 'fixture unavailable'})};
  };
  await s.voice.speakReply('Original retained after failure.', s.voice.generation);
  assert.deepEqual(calls, ['/api/spoken-version']);
  assert.equal(s.voice.pending, null);
  assert.equal(s.voice.recognitionPaused(), false);
  assert.equal(s.elements.get('stop-readback').hidden, true);
  assert.equal(s.elements.get('reply-original').textContent, 'Original retained after failure.');
  s.voice.stop();
});

test('outcome speech cannot mute an existing draft', async () => {
  const s = setup();
  await listening(s);
  s.voice.restoreDraft('Newer words', null);
  await s.voice.speak('Previous outcome', s.voice.generation);
  await s.voice.speakReply('Previous reply', s.voice.generation);
  assert.equal(s.voice.pending, null);
  assert.equal(s.voice.recognitionPaused(), false);
  assert.equal(s.voice.draft.text, 'Newer words');
  assert.equal(s.sources.length, 0);
  s.voice.stop();
});

test('prepared rendition and exact original remain independently available', async () => {
  const s = setup();
  await listening(s);
  s.document.getElementById('speech-style').value = 'spoken';
  const calls = [];
  const originalFetch = s.context.fetch;
  s.context.fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    if (url !== '/api/voice-event') calls.push({url, body});
    if (url === '/api/spoken-version') return {ok: true, json: async () =>
      ({mode: 'rendition', text: 'Converted.'})};
    return originalFetch(url, options);
  };
  await s.voice.speakReply('Exact original.', s.voice.generation);
  assert.equal(calls[0].body.mode, 'spoken');
  assert.equal(s.sockets.at(-1).messages[0].text.trim(), 'Converted.');
  assert.equal(s.elements.get('reply-original').textContent, 'Exact original.');
  s.document.getElementById('speech-style').value = 'original';
  await s.voice.speakReply('Exact original.', s.voice.stopReadback('style'));
  assert.equal(calls.length, 1);
  assert.equal(s.sockets.at(-1).messages[0].text.trim(), 'Exact original.');
  assert.deepEqual(s.submitted, []);
  s.voice.stop();
});

test('cancelled text preparation cannot start audio or populate the readback cache', async () => {
  const s = setup();
  await listening(s);
  let finish;
  const calls = [];
  s.context.fetch = async url => {
    if (url === '/api/voice-event') return {};
    calls.push(url);
    return await new Promise(resolve => { finish = resolve; });
  };
  const pending = s.voice.speakReply('Old reply.', s.voice.generation);
  s.voice.stopReadback('stop');
  finish({ok: true, json: async () => ({mode: 'original', text: 'Old reply.'})});
  await pending;
  assert.deepEqual(calls, ['/api/spoken-version']);
  assert.equal(s.voice.lastRendition, null);
  assert.equal(s.sources.length, 0);
  assert.equal(s.voice.pending, null);
  s.voice.stop();
});

test('reply is heard only after all audio finishes, never on cancel, failure or empty speech', async () => {
  const s = setup();
  await listening(s);
  s.document.getElementById('speech-style').value = 'original';
  let heard = 0;
  const play = () => s.voice.speakReply('The completed reply.', s.voice.stopReadback('next'), false, () => heard++);
  await play();
  let socket = s.sockets.at(-1);
  socket.onmessage({data: JSON.stringify({audio: btoa('\0\0\0\0'), isFinal: true})});
  assert.equal(heard, 0);
  s.sources.at(-1).onended();
  assert.equal(heard, 1);
  await play();
  socket = s.sockets.at(-1);
  socket.onmessage({data: JSON.stringify({audio: btoa('\0\0\0\0')})});
  s.voice.stopReadback('stop');
  socket.onmessage({data: JSON.stringify({isFinal: true})});
  assert.equal(heard, 1);
  await play();
  s.sockets.at(-1).onerror();
  assert.equal(heard, 1);
  await play();
  s.sockets.at(-1).onmessage({data: JSON.stringify({isFinal: true})});
  assert.equal(heard, 1);
  s.voice.stop();
});

function semanticSetup() {
  const clock = fakeClock();
  const s = setup({clock});
  const decisions = [], invalidations = [], discarded = [];
  s.voice.semantic = {
    decide: (payload, signal) => new Promise((resolve, reject) => {
      decisions.push({payload: JSON.parse(JSON.stringify(payload)), signal, resolve, reject});
    }),
    invalidate: payload => invalidations.push(JSON.parse(JSON.stringify(payload))),
  };
  s.voice.onDiscard = draft => discarded.push(draft.text);
  let ids = 0;
  s.voice.newId = () => 'tr' + ++ids;
  const receive = (message_type, text) => s.voice.capture.ws.onmessage({data: JSON.stringify({message_type, text})});
  return {...s, clock, decisions, invalidations, discarded, receive};
}

test('a thinking pause that continues stays one turn; the late judgment for the shorter text cannot submit', async () => {
  const s = semanticSetup();
  await listening(s);
  s.receive('committed_transcript', 'Tell Nova');
  await s.clock.advance(699);
  assert.equal(s.decisions.length, 0);
  await s.clock.advance(1);
  assert.equal(s.decisions.length, 1);
  assert.equal(s.decisions[0].payload.text, 'Tell Nova');
  s.receive('partial_transcript', 'and check');
  assert.equal(s.decisions[0].signal.aborted, true);
  assert.equal(s.invalidations.length, 1);
  assert.equal(s.invalidations[0].transcript_id, 'tr1');
  assert.equal(s.invalidations[0].transcript_id, s.decisions[0].payload.transcript_id);
  assert.ok(s.invalidations[0].transcript_revision > s.decisions[0].payload.transcript_revision);
  s.decisions[0].resolve({decision: 'complete', decision_id: 'd1'});
  await s.clock.advance(10);
  assert.deepEqual(s.submitted, []);
  s.receive('committed_transcript', 'and check the logs');
  await s.clock.advance(700);
  assert.equal(s.decisions.length, 2);
  assert.equal(s.decisions[1].payload.text, 'Tell Nova and check the logs');
  assert.equal(s.decisions[1].payload.transcript_id, 'tr1');
  s.decisions[1].resolve({decision: 'complete', decision_id: 'd2'});
  await s.clock.advance(0);
  assert.equal(s.submissions.length, 1);
  assert.equal(s.submissions[0].text, 'Tell Nova and check the logs');
  assert.deepEqual(s.submissions[0].meta, {transcript_id: 'tr1', transcript_revision: s.decisions[1].payload.transcript_revision, decision_id: 'd2'});
  assert.equal(s.invalidations.length, 1);
  assert.equal(s.voice.draft, null);
  s.voice.stop();
});

test('each new word or onset restarts the silence window before any judgment is requested', async () => {
  const s = semanticSetup();
  await listening(s);
  s.receive('committed_transcript', 'Hello');
  await s.clock.advance(600);
  s.receive('partial_transcript', 'there');
  s.receive('committed_transcript', 'there');
  await s.clock.advance(600);
  assert.equal(s.decisions.length, 0);
  await s.clock.advance(100);
  assert.equal(s.decisions.length, 1);
  assert.equal(s.decisions[0].payload.text, 'Hello there');
  s.voice.stop();
});

test('explicit Send submits immediately, aborts the judgment and a late complete cannot send twice', async () => {
  const s = semanticSetup();
  await listening(s);
  s.receive('committed_transcript', 'Send this now');
  await s.clock.advance(700);
  await s.voice.finishDraft(true);
  assert.equal(s.submissions.length, 1);
  assert.equal(s.submissions[0].meta.explicit_send, true);
  assert.equal('decision_id' in s.submissions[0].meta, false);
  assert.equal(s.decisions[0].signal.aborted, true);
  s.decisions[0].resolve({decision: 'complete', decision_id: 'late'});
  await s.clock.advance(10000);
  assert.equal(s.submissions.length, 1);
  s.voice.stop();
});

test('wait and failed judgments never send; rechecks back off and stop after the bound', async () => {
  const s = semanticSetup();
  await listening(s);
  s.receive('committed_transcript', 'Maybe the second one');
  await s.clock.advance(700);
  s.decisions[0].resolve({decision: 'wait'});
  await s.clock.advance(0);
  assert.equal(s.elements.get('speech-draft-status').textContent, 'Waiting for you · keep talking or press Send');
  await s.clock.advance(3999);
  assert.equal(s.decisions.length, 1);
  await s.clock.advance(1);
  assert.equal(s.decisions.length, 2);
  s.decisions[1].reject(new Error('provider down'));
  await s.clock.advance(0);
  await s.clock.advance(7999);
  assert.equal(s.decisions.length, 2);
  await s.clock.advance(1);
  assert.equal(s.decisions.length, 3);
  s.decisions[2].resolve({decision: 'wait'});
  await s.clock.advance(120000);
  assert.equal(s.decisions.length, 3);
  assert.deepEqual(s.submitted, []);
  assert.equal(s.voice.draft.text, 'Maybe the second one');
  assert.equal(s.clock.pending(), 0);
  s.voice.stop();
});

test('a complete judgment without a decision id is treated as wait', async () => {
  const s = semanticSetup();
  await listening(s);
  s.receive('committed_transcript', 'Do the thing');
  await s.clock.advance(700);
  s.decisions[0].resolve({decision: 'complete'});
  await s.clock.advance(0);
  assert.deepEqual(s.submitted, []);
  assert.equal(s.voice.draft.waiting, true);
  s.voice.stop();
});

test('a cancel judgment discards the unsent words as recoverable history without submitting', async () => {
  const s = semanticSetup();
  await listening(s);
  s.receive('committed_transcript', 'Never mind forget it');
  await s.clock.advance(700);
  s.decisions[0].resolve({decision: 'cancel'});
  await s.clock.advance(0);
  assert.deepEqual(s.submitted, []);
  assert.deepEqual(s.discarded, ['Never mind forget it']);
  assert.equal(s.voice.draft, null);
  assert.equal(s.elements.get('voice-status').textContent, 'Discarded unsent words');
  s.voice.stop();
});

test('a complete judgment after the recipient changed holds the draft instead of sending', async () => {
  const s = semanticSetup();
  let target = {pane: '%1', identity: 'one'};
  s.voice.target = () => target;
  await listening(s);
  s.receive('committed_transcript', 'Please fix it');
  await s.clock.advance(700);
  target = {pane: '%2', identity: 'two'};
  s.decisions[0].resolve({decision: 'complete', decision_id: 'd1'});
  await s.clock.advance(0);
  assert.deepEqual(s.submitted, []);
  assert.equal(s.voice.draft.held, true);
  assert.equal(s.voice.draft.text, 'Please fix it');
  s.voice.stop();
});

test('stopping voice aborts the judgment and a late complete cannot send', async () => {
  const s = semanticSetup();
  await listening(s);
  s.receive('committed_transcript', 'Almost done');
  await s.clock.advance(700);
  s.voice.stop();
  assert.equal(s.decisions[0].signal.aborted, true);
  s.decisions[0].resolve({decision: 'complete', decision_id: 'd1'});
  await s.clock.advance(60000);
  assert.deepEqual(s.submitted, []);
  assert.equal(s.voice.draft.text, 'Almost done');
});

test('direct dictation never consults the judge and keeps the captured recipient identity', async () => {
  const clock = fakeClock();
  const s = setup({clock});
  let calls = 0;
  s.voice.semantic = null;
  const target = {tab: 't1', pane: '%1', identity: 'one'};
  s.voice.target = () => target;
  await listening(s);
  s.voice.capture.ws.onmessage({data: JSON.stringify({message_type: 'committed_transcript', text: 'Run the tests'})});
  await clock.advance(2999);
  assert.equal(s.submissions.length, 0);
  await clock.advance(1);
  assert.equal(s.submissions.length, 1);
  assert.deepEqual(s.submissions[0].target, target);
  assert.equal(s.submissions[0].meta.explicit_send, false);
  assert.equal('decision_id' in s.submissions[0].meta, false);
  assert.equal(calls, 0);
  s.voice.stop();
});

function streamed(events) {
  const encoder = new TextEncoder();
  const queue = [...events];
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  return {release, response: {ok: true, headers: {get: () => 'application/x-ndjson'}, body: {getReader: () => ({
    read: async () => {
      if (!queue.length) return {done: true};
      const next = queue.shift();
      if (next === 'GATE') { await gate; return {value: new Uint8Array(), done: false}; }
      return {value: encoder.encode(JSON.stringify(next) + '\n'), done: false};
    },
    releaseLock() {},
  })}}};
}

function cancellation() {
  const s = setup();
  const urls = [];
  const original = s.context.fetch;
  s.context.fetch = (url, options) => { if (url !== '/api/voice-event') urls.push(url); return original(url, options); };
  const tracked = [];
  const track = (player, meta) => {
    const entry = {meta, progress: []};
    player.track(value => entry.progress.push(JSON.parse(JSON.stringify(value))));
    tracked.push(entry);
  };
  let heard = 0;
  return {...s, urls, tracked, track, heard: () => heard, onHeard: () => { heard++; }};
}
const ttsSocket = s => s.sockets.filter(socket => socket.url.includes('tts='));
const audioFrame = JSON.stringify({audio: btoa('\0\0\0\0')});
const noControlCalls = s => assert.deepEqual(s.urls.filter(url => /jarvis|input|request/.test(url)), []);

test('stop while the rendition is still streaming cancels the player and the rest of the stream cannot restart speech', async () => {
  const s = cancellation();
  await listening(s);
  const stream = streamed([{type: 'text', text: 'First sentence. '}, 'GATE',
    {type: 'text', text: 'Second sentence. '}, {type: 'result', result: {mode: 'rendition', text: 'First sentence. Second sentence. '}}]);
  s.context.fetch = (url, options) => url === '/api/spoken-version' ? Promise.resolve(stream.response) : Promise.resolve({ok: true});
  const generation = s.voice.generation;
  const speaking = s.voice.speakReply('Original text.', generation, false, s.onHeard, s.track);
  await new Promise(resolve => setImmediate(resolve));
  const socket = ttsSocket(s)[0];
  assert.equal(socket.messages[0].text, 'First sentence. ');
  assert.equal(s.tracked[0].meta.style, 'spoken');
  s.voice.stopReadback('stop');
  stream.release();
  await speaking;
  socket.onmessage({data: audioFrame});
  socket.onmessage({data: JSON.stringify({isFinal: true})});
  assert.equal(ttsSocket(s).length, 1);
  assert.equal(socket.messages.length, 1);
  assert.equal(s.sources.length, 0);
  assert.equal(s.heard(), 0);
  assert.equal(s.tracked[0].progress.at(-1).status, 'cancelled');
  assert.deepEqual(s.tracked[0].progress.at(-1).completed_phrases, []);
  assert.equal(s.voice.lastRendition, null);
  assert.equal(s.voice.playing, null);
  assert.deepEqual(s.submitted, []);
  s.voice.stop();
});

test('stop before first audio leaves late audio and the provider final inert', async () => {
  const s = cancellation();
  await listening(s);
  s.document.getElementById('speech-style').value = 'original';
  await s.voice.speakReply('Exact reply.', s.voice.generation, false, s.onHeard, s.track);
  const socket = ttsSocket(s)[0];
  assert.equal(s.tracked[0].progress.at(-1).status, 'generated');
  s.voice.stopReadback('stop');
  socket.onmessage({data: audioFrame});
  socket.onmessage({data: JSON.stringify({isFinal: true})});
  socket.onclose();
  assert.equal(s.sources.length, 0);
  assert.equal(s.heard(), 0);
  assert.equal(s.tracked[0].progress.at(-1).status, 'cancelled');
  assert.equal(s.elements.get('reply-original').textContent, 'Exact reply.');
  noControlCalls(s);
  s.voice.stop();
});

test('stop in the middle of audio keeps the reply unheard and late sources cannot restart it', async () => {
  const s = cancellation();
  await listening(s);
  s.document.getElementById('speech-style').value = 'original';
  await s.voice.speakReply('Exact reply.', s.voice.generation, false, s.onHeard, s.track);
  const socket = ttsSocket(s)[0];
  socket.onmessage({data: audioFrame});
  assert.equal(s.tracked[0].progress.at(-1).status, 'playing');
  s.sources[0].onended();
  socket.onmessage({data: audioFrame});
  s.voice.stopReadback('stop');
  assert.equal(s.sources[1].stopped, true);
  socket.onmessage({data: audioFrame});
  socket.onmessage({data: JSON.stringify({isFinal: true})});
  assert.equal(s.sources.length, 2);
  assert.equal(s.heard(), 0);
  const last = s.tracked[0].progress.at(-1);
  assert.equal(last.status, 'cancelled');
  assert.deepEqual(last.completed_phrases, []);
  noControlCalls(s);
  s.voice.stop();
});

test('hiding the page mid-audio and mid-generation stops everything and nothing restarts on return', async () => {
  const s = cancellation();
  await listening(s);
  s.document.getElementById('speech-style').value = 'original';
  const generation = s.voice.generation;
  await s.voice.speakReply('Exact reply.', generation, false, s.onHeard, s.track);
  const socket = ttsSocket(s)[0];
  socket.onmessage({data: audioFrame});
  s.document.hidden = true;
  s.listeners.visibilitychange();
  assert.equal(s.sources[0].stopped, true);
  assert.equal(s.voice.capture, null);
  s.document.hidden = false;
  s.listeners.visibilitychange();
  socket.onmessage({data: audioFrame});
  socket.onmessage({data: JSON.stringify({isFinal: true})});
  await s.voice.speakReply('Exact reply.', generation, false, s.onHeard, s.track);
  await s.voice.speak('Old outcome.', generation);
  assert.equal(ttsSocket(s).length, 1);
  assert.equal(s.sources.length, 1);
  assert.equal(s.heard(), 0);
  assert.equal(s.tracked[0].progress.at(-1).status, 'cancelled');
  assert.equal(s.elements.get('reply-original').textContent, 'Exact reply.');
  noControlCalls(s);
});

test('hiding the page while a rendition is generating cannot start audio when text arrives later', async () => {
  const s = cancellation();
  await listening(s);
  const stream = streamed(['GATE', {type: 'text', text: 'Late words. '}, {type: 'result', result: {mode: 'rendition', text: 'Late words. '}}]);
  s.context.fetch = url => url === '/api/spoken-version' ? Promise.resolve(stream.response) : Promise.resolve({ok: true});
  const speaking = s.voice.speakReply('Original text.', s.voice.generation, false, s.onHeard, s.track);
  await new Promise(resolve => setImmediate(resolve));
  s.document.hidden = true;
  s.listeners.visibilitychange();
  stream.release();
  await speaking;
  assert.equal(ttsSocket(s).length, 0);
  assert.equal(s.sources.length, 0);
  assert.equal(s.heard(), 0);
  assert.equal(s.tracked.length, 0);
  assert.equal(s.voice.lastRendition, null);
});

test('a late text-to-speech fetch after stop never plays or marks heard', async () => {
  const s = cancellation();
  await listening(s);
  let finish;
  s.context.fetch = (url, options) => url === '/api/speech'
    ? new Promise(resolve => { finish = resolve; }) : Promise.resolve({ok: true});
  const speaking = s.voice.speak('Old outcome.', s.voice.generation, s.onHeard);
  s.voice.stopReadback('stop');
  finish({ok: true, arrayBuffer: async () => new ArrayBuffer(8)});
  await speaking;
  assert.equal(s.sources.length, 0);
  assert.equal(s.heard(), 0);
  s.voice.stop();
});

async function localWaiting(s) {
  const detector = {trigger: false, closed: false,
    feed() { const fired = this.trigger; this.trigger = false; return fired; },
    close() { this.closed = true; }};
  s.context.LocalWakeDetector = {create: async () => detector};
  s.voice.localWake = true;
  await s.voice.start();
  return detector;
}
function microphonePacket(s, value = 7) {
  s.voice.capture.processor.port.onmessage({data: {pcm: new Int16Array(2048).fill(value), rms: .1}});
}
function recognitionMessage(socket, text, type = 'committed_transcript') {
  socket.onmessage({data: JSON.stringify({message_type: type, text})});
}

test('local wake keeps idle audio local and background RMS cannot interrupt playback', async () => {
  const s = setup({clock: fakeClock()});
  const detector = await localWaiting(s);
  await s.voice.speak('response', s.voice.generation);
  const source = s.sources[0];
  for (let i = 0; i < 50; i++) microphonePacket(s);
  assert.equal(s.sockets.length, 0);
  assert.equal(source.stopped, undefined);
  assert.ok(s.voice.capture.bufferSamples <= 48000);
  detector.trigger = true;
  microphonePacket(s, 42);
  assert.equal(source.stopped, true);
  assert.equal(s.sockets.length, 1);
  const socket = s.sockets[0];
  recognitionMessage(socket, undefined, 'session_started');
  assert.ok(socket.messages.length > 0);
  assert.equal(new Int16Array(Uint8Array.from(atob(socket.messages.at(-1).audio_base_64), x => x.charCodeAt(0)).buffer)[0], 42);
  s.voice.stop();
  assert.equal(detector.closed, true);
});

test('wake boundary rejects earlier pre-roll commits and preserves only the exact command suffix', async () => {
  const s = setup({clock: fakeClock()});
  const detector = await localWaiting(s);
  detector.trigger = true; microphonePacket(s);
  const socket = s.sockets[0];
  recognitionMessage(socket, undefined, 'session_started');
  recognitionMessage(socket, 'Tell Controller to stop.');
  recognitionMessage(socket, 'Tell Controller to stop.', 'partial_transcript');
  assert.equal(s.voice.draft, null);
  assert.equal(s.voice.capture.prefixConsumed, false);
  recognitionMessage(socket, 'Tell Controller to stop. Hey Jarvis, read Dog Data exactly.');
  assert.equal(s.voice.draft.text, 'read Dog Data exactly.');
  await s.voice.finishDraft(true);
  assert.deepEqual(s.submitted, ['read Dog Data exactly.']);
  assert.equal(socket.closed, undefined);
  assert.equal(s.voice.capture.wakeState, 'listening');
  assert.equal(s.voice.capture.ready, true);
  await s.voice.speak('verified response', s.voice.generation);
  assert.equal(s.sources.at(-1).started, true);
  s.voice.stop();
});

test('split wake boundary and wake alone never submit and the following command keeps its beginning', async () => {
  const s = setup({clock: fakeClock()});
  const detector = await localWaiting(s);
  detector.trigger = true; microphonePacket(s);
  const socket = s.sockets[0];
  recognitionMessage(socket, undefined, 'session_started');
  recognitionMessage(socket, 'background Hey');
  recognitionMessage(socket, 'Jarvis.');
  assert.equal(s.voice.draft, null);
  assert.deepEqual(s.submitted, []);
  recognitionMessage(socket, 'Explain Café 🐕 numbers.');
  assert.equal(s.voice.draft.text, 'Explain Café 🐕 numbers.');
  await s.voice.finishDraft(true);
  assert.deepEqual(s.submitted, ['Explain Café 🐕 numbers.']);
  s.voice.stop();
});

test('wake timeout returns locally and stale recognition callbacks cannot cross a new wake', async () => {
  const clock = fakeClock(), s = setup({clock});
  const detector = await localWaiting(s);
  detector.trigger = true; microphonePacket(s);
  const old = s.sockets[0], late = old.onmessage;
  recognitionMessage(old, undefined, 'session_started');
  await clock.advance(s.voice.wakeIdleMs);
  assert.equal(old.closed, true);
  assert.equal(s.voice.capture.wakeState, 'waiting');
  detector.trigger = true; microphonePacket(s);
  const fresh = s.sockets[1];
  late({data: JSON.stringify({message_type: 'committed_transcript', text: 'Hey Jarvis, stale command'})});
  assert.equal(s.voice.draft, null);
  recognitionMessage(fresh, undefined, 'session_started');
  recognitionMessage(fresh, 'Hey Jarvis.');
  assert.deepEqual(s.submitted, []);
  s.voice.stop();
});

test('microphone loss during model load disposes late initialization and cannot restore capture', async () => {
  const s = setup({clock: fakeClock()});
  let finish;
  s.context.LocalWakeDetector = {create: () => new Promise(resolve => { finish = resolve; })};
  s.voice.localWake = true;
  const starting = s.voice.start();
  for (let i = 0; i < 12; i++) await Promise.resolve();
  assert.equal(typeof s.tracks[0].onended, 'function');
  s.tracks[0].onended();
  const detector = {closed: false, close() { this.closed = true; }};
  finish(detector); await starting;
  assert.equal(s.voice.capture, null);
  assert.equal(detector.closed, true);
  assert.equal(s.sockets.length, 0);
});

test('wake idle can resume only its own finalized paused voice draft without another mic tap', async () => {
  const clock = fakeClock(), s = setup({clock});
  s.voice.semantic = {invalidate() {}, decide: async () => ({decision: 'wait'})};
  const detector = await localWaiting(s);
  detector.trigger = true; microphonePacket(s);
  recognitionMessage(s.sockets[0], undefined, 'session_started');
  recognitionMessage(s.sockets[0], 'Hey Jarvis, explain those numbers');
  await clock.advance(s.voice.wakeIdleMs);
  assert.equal(s.voice.draft.wakeResume, true);
  detector.trigger = true; microphonePacket(s);
  assert.equal(s.sockets.length, 2);
  recognitionMessage(s.sockets[1], undefined, 'session_started');
  recognitionMessage(s.sockets[1], 'Hey Jarvis, with an example.');
  assert.equal(s.voice.draft.text, 'explain those numbers with an example.');
  assert.deepEqual(s.submitted, []);
  s.voice.stop();
  assert.equal(s.voice.draft.wakeResume, false);
});

test('wake cannot merge a restored draft and a failed model never falls back to cloud capture', async () => {
  const s = setup({clock: fakeClock()});
  const detector = await localWaiting(s);
  s.voice.restoreDraft('retained unrelated words', null);
  detector.trigger = true; microphonePacket(s);
  assert.equal(s.sockets.length, 0);
  assert.equal(s.voice.draft.text, 'retained unrelated words');
  s.voice.stop();
  s.context.LocalWakeDetector = {create: async () => { throw new Error('Local model unavailable'); }};
  await s.voice.start();
  assert.equal(s.voice.capture, null);
  assert.equal(s.sockets.length, 0);
  assert.equal(s.document.getElementById('voice-status').textContent, 'Local model unavailable');
});

test('wake resume remains bound to the real Jarvis session and subject, not the generic Jarvis target', async () => {
  for (const changed of ['subject', 'session', 'unchanged']) {
    const clock = fakeClock(), s = setup({clock});
    vm.runInContext(readFileSync(new URL('./jarvis.js', import.meta.url), 'utf8') + '\nglobalThis.Client = JarvisClient;', s.context);
    const requests = [];
    let complete = false;
    const client = new s.context.Client({newId: () => 'id', retain() {}, clientId: 'phone',
      fetch: async (url, options) => {
        const payload = JSON.parse(options.body); requests.push(payload);
        return {ok: true, json: async () => ({...payload, decision: complete ? 'complete' : 'wait',
          ...(complete ? {decision_id: 'accepted'} : {})})};
      }});
    const dog = {tab: 'dog', pane: 'dog', identity: 'dog-A'}, controller = {tab: 'controller', pane: 'controller', identity: 'controller-A'};
    client.adopt({session_id: 'session', revision: 1, subject: dog, turns: {}, actions: {}});
    s.voice.target = () => client.target;
    s.voice.semantic = client.semantic;
    client.voice = s.voice;
    const detector = await localWaiting(s);
    detector.trigger = true; microphonePacket(s);
    recognitionMessage(s.sockets[0], undefined, 'session_started');
    recognitionMessage(s.sockets[0], 'Hey Jarvis, tell it the numbers');
    await clock.advance(s.voice.wakeIdleMs);
    assert.equal(s.voice.draft.wakeResume, true);
    client.adopt({session_id: changed === 'session' ? 'restarted' : 'session', revision: 2,
      subject: changed === 'subject' ? controller : dog, turns: {}, actions: {}});
    complete = true;
    detector.trigger = true; microphonePacket(s);
    if (changed === 'unchanged') {
      recognitionMessage(s.sockets[1], undefined, 'session_started');
      recognitionMessage(s.sockets[1], 'Hey Jarvis, look correct.');
      await clock.advance(s.voice.silenceMs);
      assert.deepEqual(s.submitted, ['tell it the numbers look correct.']);
      assert.deepEqual(JSON.parse(JSON.stringify(requests.at(-1).subject)), dog);
    } else {
      assert.equal(s.sockets.length, 1);
      assert.deepEqual(s.submitted, []);
      assert.equal(s.voice.draft.text, 'tell it the numbers');
      assert.equal(s.voice.draft.held, true);
    }
    s.voice.stop();
  }
});

test('one wake supports separate followups, then idle closes cloud and requires a new boundary', async () => {
  const clock = fakeClock(), s = setup({clock});
  const detector = await localWaiting(s);
  detector.trigger = true; microphonePacket(s);
  const socket = s.sockets[0];
  recognitionMessage(socket, undefined, 'session_started');
  recognitionMessage(socket, 'Hey Jarvis, read Dog Data.');
  await s.voice.finishDraft(true);
  await clock.advance(50000);
  recognitionMessage(socket, 'Explain those numbers with an example.');
  await s.voice.finishDraft(true);
  assert.deepEqual(s.submitted, ['read Dog Data.', 'Explain those numbers with an example.']);
  assert.equal(s.sockets.length, 1);
  assert.equal(socket.closed, undefined);
  await clock.advance(59999);
  assert.equal(s.voice.capture.wakeState, 'listening');
  await clock.advance(1);
  assert.equal(socket.closed, true);
  assert.equal(s.voice.capture.wakeState, 'waiting');
  microphonePacket(s);
  assert.equal(s.sockets.length, 1);
  detector.trigger = true; microphonePacket(s);
  recognitionMessage(s.sockets[1], undefined, 'session_started');
  recognitionMessage(s.sockets[1], 'unaddressed pre-roll speech');
  assert.equal(s.voice.draft, null);
  recognitionMessage(s.sockets[1], 'Hey Jarvis, next request.');
  assert.equal(s.voice.draft.text, 'next request.');
  s.voice.stop();
});

test('pending response and playback leave a full conversation window after audio finishes', async () => {
  const clock = fakeClock(), s = setup({clock});
  const detector = await localWaiting(s);
  detector.trigger = true; microphonePacket(s);
  recognitionMessage(s.sockets[0], undefined, 'session_started');
  recognitionMessage(s.sockets[0], 'Hey Jarvis, explain the output.');
  let resolve;
  s.voice.submit = () => new Promise(done => {resolve = done;});
  const submitting = s.voice.finishDraft(true);
  await clock.advance(120000);
  assert.equal(s.voice.capture.wakeState, 'listening');
  resolve(); await submitting;
  await s.voice.speak('reply', s.voice.generation);
  await clock.advance(120000);
  assert.equal(s.voice.capture.wakeState, 'listening');
  s.sources[0].onended();
  await clock.advance(59999);
  assert.equal(s.voice.capture.wakeState, 'listening');
  await clock.advance(1);
  assert.equal(s.voice.capture.wakeState, 'waiting');
  s.voice.stop();
});

test('automatic activation tries granted, prompt and unsupported permission queries once; denied and suspended stay off', async () => {
  for (const state of ['granted', 'prompt', 'unsupported', 'denied', 'suspended']) {
    const s = setup({clock: fakeClock()});
    let grants = 0;
    s.voice.localWake = true;
    s.voice.autoAttempted = false;
    s.context.LocalWakeDetector = {create: async () => ({feed() {return false;}, close() {}})};
    s.context.navigator.permissions = state === 'unsupported' ? undefined : {query: async () => ({state: state === 'suspended' ? 'granted' : state})};
    s.context.navigator.mediaDevices.getUserMedia = async () => {grants++;return s.stream;};
    if (state === 'suspended') {
      const Context = s.context.AudioContext;
      s.context.AudioContext = class extends Context {state = 'suspended';};
    }
    s.voice.configure({configured: true, websocket_url: 'ws://test'});
    for (let i = 0; i < 30; i++) await Promise.resolve();
    assert.equal(grants, state === 'denied' ? 0 : 1);
    assert.equal(!!s.voice.capture, !['denied', 'suspended'].includes(state));
    assert.equal(s.sockets.length, 0);
    s.voice.stop();
    s.voice.configure({configured: true, websocket_url: 'ws://test'});
    for (let i = 0; i < 10; i++) await Promise.resolve();
    assert.equal(grants, state === 'denied' ? 0 : 1);
  }
});

test('Stop before delayed automatic permission or microphone grant prevents late capture', async () => {
  for (const delayed of ['permission', 'microphone']) {
    const s = setup({clock: fakeClock()});
    let finish, grants = 0, models = 0;
    s.voice.localWake = true; s.voice.autoAttempted = false;
    s.context.LocalWakeDetector = {create: async () => {models++;return {feed() {return false;}, close() {}};}};
    s.context.navigator.permissions = {query: delayed === 'permission' ? () => new Promise(resolve => {finish = resolve;}) : async () => ({state:'granted'})};
    s.context.navigator.mediaDevices.getUserMedia = () => {grants++;return delayed === 'microphone' ? new Promise(resolve => {finish = resolve;}) : Promise.resolve(s.stream);};
    s.voice.configure({configured:true,websocket_url:'ws://test'});
    for(let i=0;i<15;i++) await Promise.resolve();
    s.voice.stop();
    finish(delayed === 'permission' ? {state:'granted'} : s.stream);
    for(let i=0;i<20;i++) await Promise.resolve();
    assert.equal(s.voice.capture, null);
    assert.equal(grants, delayed === 'permission' ? 0 : 1);
    assert.equal(models, 0);
    assert.equal(s.sockets.length, 0);
    if(delayed === 'microphone') assert.equal(s.tracks[0].stopped, true);
  }
});

test('repeated wake while awake stops readback but wake alone produces no request and queued wake audio is retained', async () => {
  const s = setup({clock:fakeClock()});
  const detector = await localWaiting(s);
  detector.trigger = true; microphonePacket(s);
  const socket = s.sockets[0];
  recognitionMessage(socket, undefined, 'session_started');
  recognitionMessage(socket, 'Hey Jarvis, first command.');
  await s.voice.finishDraft(true);
  await s.voice.speak('reply', s.voice.generation);
  microphonePacket(s, 41);
  detector.trigger = true; microphonePacket(s, 42);
  assert.equal(s.sources[0].stopped, true);
  assert.equal(new Int16Array(Uint8Array.from(atob(socket.messages.at(-1).audio_base_64), x => x.charCodeAt(0)).buffer)[0],42);
  recognitionMessage(socket, 'echo before Hey Jarvis.');
  assert.equal(s.voice.draft, null);
  assert.deepEqual(s.submitted, ['first command.']);
  recognitionMessage(socket, 'Ask Dog Data: include Hey Jarvis in the greeting.');
  assert.equal(s.voice.draft.text, 'Ask Dog Data: include Hey Jarvis in the greeting.');
  s.voice.stop();
});

test('typed visual reference stays captured while preview changes and never changes recipient or literal words', async () => {
  const s = setup();
  const target = {tab: '@a', pane: '%a', identity: 'a:original'};
  let reference = {workspace_id: 'wa', creation_id: 'report', version_id: 'v1', source: {thread_id: 'other-author'}};
  s.voice.target = () => target;
  s.voice.reference = () => reference;
  s.voice.restoreDraft('Change only this sentence.  Keep spacing.', target);
  reference = {...reference, version_id: 'v2'};
  await s.voice.finishDraft(true);
  assert.equal(s.submissions[0].meta.visual_reference.version_id, 'v1');
  assert.deepEqual(s.submissions[0].target, target);
  assert.equal(s.submissions[0].text, 'Change only this sentence.  Keep spacing.');
  s.voice.stop();
});

test('speech reference binds at onset even when preview changes before the transcript', async () => {
  const s = setup();
  let reference = {workspace_id: 'wa', creation_id: 'report', version_id: 'v1'};
  s.voice.reference = () => reference;
  s.voice.speechOnset(true);
  reference = {...reference, version_id: 'v2'};
  s.voice.committed('Revise this report');
  await s.voice.finishDraft(true);
  assert.equal(s.submissions[0].meta.visual_reference.version_id, 'v1');
  s.voice.stop();
});
