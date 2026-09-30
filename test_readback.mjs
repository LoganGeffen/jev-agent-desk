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
