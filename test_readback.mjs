import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function setup() {
  const sockets = [], sources = [], events = [];
  class Socket {
    static OPEN = 1;
    readyState = 1;
    messages = [];
    constructor() { sockets.push(this); }
    send(text) { this.messages.push(JSON.parse(text)); }
    close() { this.closed = true; }
  }
  const audio = {
    currentTime: 10, destination: {},
    createBuffer: (_, count, rate) => ({duration: count / rate, getChannelData: () => new Float32Array(count)}),
    createBufferSource() {
      const source = {connect() {}, disconnect() {}, stop() { this.stopped = true; }, start(at) { this.at = at; }};
      sources.push(source); return source;
    },
  };
  const context = vm.createContext({WebSocket: Socket, Uint8Array, DataView, TextDecoder, atob, setTimeout, clearTimeout});
  vm.runInContext(readFileSync(new URL('./readback.js', import.meta.url), 'utf8') + '\nglobalThis.Player = ReadbackPlayer;', context);
  const player = new context.Player('ws://fixture', audio, {
    onStart: () => events.push('start'), onEnd: () => events.push('end'), onError: error => events.push(error.message),
  });
  return {player, socket: sockets[0], sources, events, context};
}
const audio = JSON.stringify({audio: Buffer.alloc(4800).toString('base64')});

test('streams a complete short phrase before a sentence and flushes a partial tail', () => {
  const s = setup();
  s.player.write('There are two ready chats:');
  assert.equal(s.socket.messages.length, 0);
  s.player.write(' Dog Da');
  assert.equal(s.socket.messages[0].text, 'There are two ready chats: ');
  s.player.write('ta is waiting for your ');
  assert.equal(s.socket.messages[1].text, 'Dog Data is waiting for your ');
  s.player.write('input');
  s.player.end();
  assert.equal(s.socket.messages[2].text, 'input ');
  assert.equal(s.socket.messages[3].text, '');
  s.player.stop();
});

test('six-word threshold wins over later punctuation and preserves numbers and identifiers', () => {
  const s = setup();
  const text = 'Use https://example.test:8443 and sample_id=ABC_123 for 1,234 dogs, then wait.';
  s.player.write(text);
  assert.equal(s.socket.messages[0].text, 'Use https://example.test:8443 and sample_id=ABC_123 for 1,234 ');
  assert.equal(s.socket.messages[1].text, 'dogs, ');
  s.player.end();
  assert.equal(s.socket.messages.map(m => m.text).join(''), text + ' ');
  s.player.stop();
});

test('phrase buffering preserves text across every delta width', () => {
  const text = 'Dog Data: 1,234 dogs; 40 have a trait. Ready? Final tail';
  for (let width = 1; width <= text.length; width++) {
    const s = setup();
    for (let i = 0; i < text.length; i += width) s.player.write(text.slice(i, i + width));
    s.player.end();
    assert.equal(s.socket.messages.map(m => m.text).join(''), text + ' ');
    s.player.stop();
  }
});

test('plays first audio before text completion and waits for scheduled audio to end', () => {
  const s = setup();
  s.player.write('First sentence. Next');
  assert.equal(s.socket.messages[0].text, 'First sentence. ');
  s.socket.onmessage({data: audio});
  assert.equal(s.sources.length, 1);
  assert.deepEqual(s.events, ['start']);
  s.player.write(' sentence.');
  s.player.end();
  s.socket.onmessage({data: audio});
  assert.ok(s.sources[1].at >= s.sources[0].at + .1);
  s.socket.onmessage({data: '{"isFinal":true}'});
  assert.deepEqual(s.events, ['start']);
  s.sources[0].onended(); s.sources[1].onended();
  assert.deepEqual(s.events, ['start', 'end']);
});

test('cancel stops every queued chunk and ignores late audio', () => {
  const s = setup();
  s.socket.onmessage({data: audio}); s.socket.onmessage({data: audio});
  s.player.stop();
  assert.ok(s.sources.every(source => source.stopped));
  s.socket.onmessage({data: audio});
  s.socket.onmessage({data: '{"isFinal":true}'});
  assert.equal(s.sources.length, 2);
  assert.deepEqual(s.events, ['start']);
});

test('unfinished socket closure is an error, never a completed readback', () => {
  const s = setup();
  s.socket.onmessage({data: audio});
  s.socket.onclose();
  assert.equal(s.events.at(-1), 'Speech ended before completion');
  assert.ok(s.sources[0].stopped);
});

test('NDJSON exposes text before outcome and rejects a truncated result', async () => {
  const s = setup(); s.player.stop();
  let release;
  const encoder = new TextEncoder();
  const received = [];
  const body = new ReadableStream({start(controller) {
    controller.enqueue(encoder.encode('{"type":"text","text":"First sentence. "}\n'));
    release = () => { controller.enqueue(encoder.encode('{"type":"result","result":{"text":"First sentence."}}\n')); controller.close(); };
  }});
  const response = new Response(body, {headers: {'content-type': 'application/x-ndjson'}});
  const result = s.context.readJSONStream(response, text => received.push(text));
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(received, ['First sentence. ']);
  release();
  assert.equal((await result).text, 'First sentence.');
  await assert.rejects(s.context.readJSONStream(new Response('{"type":"text","text":"Partial"}\n',
    {headers: {'content-type': 'application/x-ndjson'}}), () => {}), /before its outcome/);
});

function tracked() {
  const s = setup();
  const progress = [];
  s.player.track(value => progress.push(JSON.parse(JSON.stringify(value))));
  return {...s, progress};
}

test('progress keeps partial and cancelled playback unknown and never lists a completed phrase', () => {
  const s = tracked();
  s.player.write('First sentence. Second');
  s.socket.onmessage({data: audio});
  s.sources[0].onended();
  assert.equal(s.progress.at(-1).status, 'playing');
  assert.deepEqual(s.progress.at(-1).completed_phrases, []);
  assert.deepEqual(s.progress.at(-1).phrase_ids, ['p1']);
  assert.equal(s.player.progress().audio_ended_ms, 100);
  s.player.stop();
  const last = s.progress.at(-1);
  assert.equal(last.status, 'cancelled');
  assert.deepEqual(last.completed_phrases, []);
  assert.equal(last.text_complete, false);
});

test('a reply is completed only after end, provider final and every scheduled source ended', () => {
  const s = tracked();
  s.player.write('One two. ');
  s.player.end();
  s.socket.onmessage({data: audio});
  s.socket.onmessage({data: audio});
  s.socket.onmessage({data: '{"isFinal":true}'});
  s.sources[0].onended();
  assert.notEqual(s.progress.at(-1).status, 'completed');
  assert.equal(s.progress.at(-1).text_complete, true);
  s.sources[1].onended();
  const last = s.progress.at(-1);
  assert.equal(last.status, 'completed');
  assert.deepEqual(last.completed_phrases, last.phrase_ids);
  assert.equal(last.text, 'One two. ');
  assert.deepEqual(s.events, ['start', 'end']);
});

test('a provider final before the reply text was fully sent fails and never completes', () => {
  const s = tracked();
  s.player.write('Long reply still arriving. ');
  s.socket.onmessage({data: audio});
  s.socket.onmessage({data: '{"isFinal":true}'});
  assert.equal(s.progress.at(-1).status, 'failed');
  assert.deepEqual(s.progress.at(-1).completed_phrases, []);
  assert.equal(s.events.at(-1), 'Speech ended before the reply was complete');
  assert.ok(!s.events.includes('end'));
});

test('a provider final with no audio fails rather than completing', () => {
  const s = tracked();
  s.player.write('Silent. ');
  s.player.end();
  s.socket.onmessage({data: '{"isFinal":true}'});
  assert.equal(s.progress.at(-1).status, 'failed');
  assert.deepEqual(s.progress.at(-1).completed_phrases, []);
});

test('late audio and final messages after a stop change neither progress nor status', () => {
  const s = tracked();
  s.player.write('Hello there. ');
  s.player.end();
  s.socket.onmessage({data: audio});
  s.player.stop();
  const count = s.progress.length;
  s.socket.onmessage({data: audio});
  s.socket.onmessage({data: '{"isFinal":true}'});
  s.socket.onclose();
  s.player.write('more'); s.player.end();
  assert.equal(s.progress.length, count);
  assert.equal(s.progress.at(-1).status, 'cancelled');
  assert.equal(s.sources.length, 1);
  assert.deepEqual(s.events, ['start']);
});

test('tracking an already playing player reports the current state immediately', () => {
  const s = setup();
  s.socket.onmessage({data: audio});
  const seen = [];
  s.player.track(value => seen.push(value.status));
  assert.deepEqual(seen, ['playing']);
  s.player.stop();
  assert.equal(seen.at(-1), 'cancelled');
});

const pcm = Buffer.alloc(4800).toString('base64');
const frame = (text, start, per, extra = {}) => JSON.stringify({audio: pcm, alignment: {chars: [...text],
  starts: [...text].map((_, i) => start + i * per), durations: [...text].map(() => per)}, ...extra});

test('aligned phrases count only after their audio fully ended, with a safety margin, and never from scheduled audio', () => {
  const s = tracked();
  s.player.write('Alpha beta. ');
  s.player.write('Gamma delta. ');
  s.player.end();
  assert.deepEqual([...s.player.phrases], ['Alpha beta. ', 'Gamma delta. ']);
  s.socket.onmessage({data: frame('Alpha beta. ', 0, 8)});
  s.socket.onmessage({data: frame('Gamma delta. ', 96, 8)});
  assert.deepEqual([...s.player.progress().aligned_phrase_ids], []);
  s.sources[0].onended();
  const first = s.player.progress();
  assert.equal(first.audio_ended_ms, 100);
  assert.deepEqual([...first.aligned_phrase_ids], []);
  assert.deepEqual([...first.completed_phrases], []);
  s.player.stop();
  assert.deepEqual([...s.player.progress().aligned_phrase_ids], []);
});

test('aligned phrase ids advance with ended audio but completed_phrases stays empty until full playback', () => {
  const s = tracked();
  s.player.write('Hi there. ');
  const long = frame('Hi there. ', 0, 4);
  s.player.ended = 0;
  s.socket.onmessage({data: long});
  s.player.endedMs = 120;
  assert.deepEqual([...s.player.progress().aligned_phrase_ids], ['p1']);
  assert.deepEqual([...s.player.progress().completed_phrases], []);
  s.player.stop();
});

test('alignment that diverges from the sent text or goes backwards is ignored and never claims phrases', () => {
  for (const make of [() => frame('Other text. ', 0, 8), () => JSON.stringify({audio: pcm, alignment: {chars: ['H'], starts: [0], durations: []}})]) {
    const s = tracked();
    s.player.write('Hi there. ');
    s.socket.onmessage({data: make()});
    s.player.endedMs = 100000;
    assert.equal(s.player.progress().alignment_valid, false);
    assert.deepEqual([...s.player.progress().aligned_phrase_ids], []);
    s.player.stop();
  }
  const s = tracked();
  s.player.write('Hi there. ');
  s.socket.onmessage({data: frame('Hi ', 100, 4)});
  s.socket.onmessage({data: frame('there. ', 0, 4)});
  s.player.endedMs = 100000;
  assert.deepEqual([...s.player.progress().aligned_phrase_ids], []);
  s.player.stop();
});

const vectors = JSON.parse(readFileSync(new URL('./test_prefix_vectors.json', import.meta.url), 'utf8')).vectors;
const pcmMs = ms => Buffer.alloc(ms * 48).toString('base64');
const timed = (elements, start, per, ms) => JSON.stringify({audio: pcmMs(ms), alignment: {chars: elements,
  starts: elements.map((_, i) => start + i * per), durations: elements.map(() => per)}});

function playsPhrases(text, elements = [...text]) {
  const s = tracked();
  s.player.write(text);
  s.player.end();
  s.socket.onmessage({data: timed(elements, 0, 10, elements.length * 10)});
  return s;
}

test('the heard prefix is the exact source through the last phrase whose audio ended, counted in code points', () => {
  const s = playsPhrases('Café 🐕. Next one. ');
  assert.deepEqual([...s.player.phrases], ['Café 🐕. ', 'Next one. ']);
  assert.equal(s.player.progress().heard_prefix, null);
  s.sources[0].onended();
  const prefix = s.player.progress().heard_prefix;
  assert.equal(prefix.text, 'Café 🐕. ');
  assert.equal(prefix.chars, 8);
  assert.equal(prefix.text.length, 9);
  assert.equal(prefix.alignment, 'provider');
  assert.deepEqual([...s.player.progress().aligned_phrase_ids], ['p1']);
  assert.deepEqual([...s.player.progress().completed_phrases], []);
  assert.equal(s.progress.at(-1).heard_prefix.chars, 8);
  s.player.stop();
});

test('provider elements may split an astral character; phrase ends still map by UTF-16 offset', () => {
  const s = playsPhrases('Café 🐕. Next one. ', 'Café 🐕. '.split('').concat(...['Next one. '].map(t => [...t])));
  s.sources[0].onended();
  assert.equal(s.player.progress().heard_prefix.chars, 8);
  s.player.stop();
});

test('a provider element that straddles a phrase boundary proves nothing', () => {
  const s = playsPhrases('Café 🐕. Next one. ', ['Café 🐕. N', ...[...'ext one. ']]);
  s.sources[0].onended();
  assert.equal(s.player.progress().heard_prefix, null);
  assert.deepEqual([...s.player.progress().aligned_phrase_ids], []);
  s.player.stop();
});

test('scheduled audio, an unfinished source and the stop moment never extend the prefix', () => {
  const s = tracked();
  s.player.write('Alpha beta. Gamma delta. ');
  s.player.end();
  s.socket.onmessage({data: timed([...'Alpha beta. '], 0, 10, 200)});
  s.socket.onmessage({data: timed([...'Gamma delta. '], 200, 10, 200)});
  assert.equal(s.player.progress().audio_scheduled_ms, 400);
  assert.equal(s.player.progress().heard_prefix, null);
  s.sources[0].onended();
  assert.equal(s.player.progress().heard_prefix.text, 'Alpha beta. ');
  s.player.stop();
  assert.equal(s.sources[1].stopped, true);
  const last = s.progress.at(-1);
  assert.equal(last.status, 'cancelled');
  assert.equal(last.heard_prefix.text, 'Alpha beta. ');
  assert.deepEqual([...last.completed_phrases], []);
  s.sources[1].onended?.();
  assert.equal(s.player.progress().heard_prefix.text, 'Alpha beta. ');
});

test('the prefix never includes the tail space the player appends and stays within the app source', () => {
  const s = tracked();
  s.player.write('One two');
  s.player.end();
  s.socket.onmessage({data: timed([...'One two '], 0, 10, 200)});
  s.sources[0].onended();
  assert.equal(s.player.progress().heard_prefix.text, 'One two');
  s.player.stop();
});

test('chunk-relative or backwards timing makes every later phrase unknown', () => {
  const s = tracked();
  s.player.write('Alpha beta. Gamma delta. ');
  s.player.end();
  s.socket.onmessage({data: timed([...'Alpha beta. '], 0, 10, 120)});
  s.socket.onmessage({data: timed([...'Gamma delta. '], 0, 10, 130)});
  s.sources[0].onended();
  s.sources[1].onended();
  assert.equal(s.player.progress().alignment_valid, false);
  assert.equal(s.player.progress().heard_prefix, null);
  s.player.stop();
});

test('alignment text that is not the exact sent text, or a missing element, is never mapped', () => {
  for (const elements of [[...'Alpha beta! '], [...'Alpha bet']]) {
    const s = tracked();
    s.player.write('Alpha beta. Gamma delta. ');
    s.socket.onmessage({data: timed(elements, 0, 5, 120)});
    s.sources[0].onended();
    assert.equal(s.player.progress().heard_prefix, null, elements.join(''));
    s.player.stop();
  }
});

test('the conservative margin grows with elapsed time so drift cannot count an unfinished phrase as heard', () => {
  const s = tracked();
  s.player.write('Alpha beta. Gamma delta. ');
  s.player.end();
  s.socket.onmessage({data: timed([...'Alpha beta. '], 0, 1000, 12000)});
  s.player.endedMs = 12000 + 50;
  assert.equal(s.player.progress().heard_prefix, null);
  s.player.endedMs = 12000 + 50 + 120;
  assert.equal(s.player.progress().heard_prefix.text, 'Alpha beta. ');
  s.player.stop();
});

test('prefix progress reports only when it advances and never after the stop', () => {
  const s = tracked();
  s.player.write('Alpha beta. Gamma delta. ');
  s.player.end();
  s.socket.onmessage({data: timed([...'Alpha beta. '], 0, 10, 200)});
  s.socket.onmessage({data: timed([...'Gamma delta. '], 200, 10, 200)});
  const before = s.progress.length;
  s.sources[0].onended();
  assert.equal(s.progress.length, before + 1);
  assert.equal(s.progress.at(-1).heard_prefix.chars, 12);
  s.player.stop();
  const stopped = s.progress.length;
  s.socket.onmessage({data: timed([...'Gamma delta. '], 200, 10, 200)});
  assert.equal(s.progress.length, stopped);
});

test('the shared Unicode vectors are exact code point prefixes of their sources', () => {
  for (const vector of vectors) {
    const points = Array.from(vector.source);
    assert.equal(points.slice(0, vector.chars).join(''), vector.prefix, vector.name);
    assert.equal(Array.from(vector.prefix).length, vector.chars, vector.name);
  }
  const astral = vectors.find(vector => vector.name === 'astral ends the prefix');
  assert.notEqual(astral.source.slice(0, astral.chars), astral.prefix);
});
