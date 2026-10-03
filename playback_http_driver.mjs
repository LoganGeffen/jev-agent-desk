import {readFileSync} from 'node:fs';
import {webcrypto} from 'node:crypto';
import vm from 'node:vm';

const config = JSON.parse(readFileSync(0, 'utf8'));
const read = name => readFileSync(new URL(name, import.meta.url), 'utf8');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const until = async check => { for (let i = 0; i < 500 && !check(); i++) await pause(2); if (!check()) throw new Error('timed out'); };
const frame = (text, start, ms) => {
  const chars = [...text];
  return {data: JSON.stringify({audio: Buffer.alloc(ms * 48).toString('base64'),
    alignment: {chars, starts: chars.map((_, index) => start + index * 10), durations: chars.map(() => 10)}})};
};

const elements = new Map(), sockets = [], sources = [], exchanges = [];
const document = {hidden: false,
  getElementById(id) { if (!elements.has(id)) elements.set(id, {setAttribute(name, value) { this[name] = value; }}); return elements.get(id); },
  addEventListener() {}};
class Context {
  currentTime = 0; sampleRate = 16000; destination = {};
  audioWorklet = {addModule: async () => {}};
  resume = async () => {}; close = async () => {};
  createMediaStreamSource = () => ({connect() {}, disconnect() {}});
  createBuffer = (_, length, rate) => ({duration: length / rate, getChannelData: () => new Float32Array(length)});
  createBufferSource() {
    const source = {connect() {}, disconnect() {}, start() {}, stop() { this.stopped = true; }};
    sources.push(source); return source;
  }
}
class Socket {
  static OPEN = 1;
  readyState = 1; bufferedAmount = 0;
  constructor(url) { this.url = url; this.messages = []; sockets.push(this); }
  send(text) { this.messages.push(JSON.parse(text)); }
  close() {}
}
const fetch = async (url, options = {}) => {
  const response = await globalThis.fetch(config.base + url, {...options, headers: {...options.headers, Origin: config.base}});
  if (url === '/api/jarvis/playback') {
    const copy = response.clone();
    exchanges.push({body: JSON.parse(options.body), status: response.status, result: await copy.json()});
  }
  return response;
};
const context = vm.createContext({document, window: {addEventListener() {}},
  navigator: {mediaDevices: {getUserMedia: async () => ({getTracks: () => []})}}, AudioContext: Context, WebSocket: Socket,
  AudioWorkletNode: class { port = {}; connect() {} disconnect() {} }, AbortController, setTimeout, clearTimeout, Date, Uint8Array, DataView,
  TextDecoder, TextEncoder, btoa, atob, fetch, crypto: webcrypto});
vm.runInContext(read('readback.js') + read('voice.js') + read('jarvis.js') + '\nglobalThis.Controls = VoiceControls; globalThis.JarvisClient = JarvisClient;', context);
const voice = new context.Controls(async () => {});
voice.configure({configured: true, websocket_url: 'ws://test'});
document.getElementById('speech-style').value = 'original';
await voice.start();
voice.capture.ws.onmessage({data: JSON.stringify({message_type: 'session_started'})});
let ids = 0;
const client = new context.JarvisClient({fetch, newId: () => 'r' + ++ids, now: () => Date.now(), deadlineMs: 5000, lostGraceMs: 500, retain() {}, onChange() {}});
client.voice = voice;
await client.refresh();
if (client.sessionId !== config.session_id) throw new Error('session mismatch');

const generation = voice.generation;
await voice.speakReply(config.original, generation, false, null, client.tracker(config.turn_id, generation));
const socket = sockets.at(-1);
const phrases = socket.messages.map(message => message.text).filter(Boolean);
let start = 0;
for (const phrase of phrases) { socket.onmessage(frame(phrase, start, phrase.length * 10 + 150)); start += phrase.length * 10 + 150; }
if (config.scenario === 'partial') {
  sources[0].onended();
  await until(() => exchanges.some(exchange => exchange.body.op === 'update'));
  await pause(100);
  voice.stopReadback('button');
  await until(() => exchanges.some(exchange => exchange.body.status === 'cancelled'));
} else {
  socket.onmessage({data: '{"isFinal":true}'});
  for (const source of sources) source.onended();
  await until(() => exchanges.some(exchange => exchange.body.status === 'completed'));
}
await pause(100);
console.log(JSON.stringify({phrases, exchanges}));
process.exit(0);
