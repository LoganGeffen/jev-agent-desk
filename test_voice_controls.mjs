import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function setup() {
  const elements = new Map();
  const listeners = {};
  const logs = [];
  const submitted = [];
  const sources = [];
  const sockets = [];
  const tracks = [{stopped: false, stop() { this.stopped = true; }}];
  const stream = {getTracks: () => tracks};
  class Context {
    currentTime = 0;
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
    AbortController, setTimeout, clearTimeout, Uint8Array, DataView, TextDecoder, btoa, atob,
    fetch: async (url, options) => {
      if (url === '/api/voice-event') logs.push(JSON.parse(options.body));
      return {ok: true, arrayBuffer: async () => new ArrayBuffer(8)};
    }
  });
  vm.runInContext(readFileSync(new URL('./readback.js', import.meta.url), 'utf8'), context);
  vm.runInContext(readFileSync(new URL('./voice.js', import.meta.url), 'utf8') + '\nglobalThis.Controls = VoiceControls;', context);
  const voice = new context.Controls(async text => { submitted.push(text); });
  voice.configure({configured: true, websocket_url: 'ws://test'});
  return {voice, context, document, elements, listeners, logs, submitted, sources, tracks, stream, sockets};
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
  assert.deepEqual(calls, []);
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
