import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const listeners = {};
const shown = [];
const navigated = [];
const focused = [];
const windowClient = {url: 'https://desk.example.test/', navigate: async url => navigated.push(url),
  focus: async () => focused.push(true)};
const self = {
  location: {origin: 'https://desk.example.test'},
  registration: {showNotification: async (...args) => shown.push(args)},
  clients: {matchAll: async () => [windowClient], openWindow: async () => { throw new Error('should focus existing'); }},
  addEventListener: (type, callback) => { listeners[type] = callback; }
};
vm.runInNewContext(fs.readFileSync('service-worker.js', 'utf8'), {self, URL});
assert.deepEqual(Object.keys(listeners).sort(), ['notificationclick', 'push']);

let pending;
listeners.push({data: {json: () => ({title: 'Build', body: 'Agent replied', event_id: 'reply:one',
  url: '/?notification=reply%3Aone&tab=%401&pane=%252&identity=exact'})}, waitUntil: value => { pending = value; }});
await pending;
assert.equal(shown[0][0], 'Build');
assert.equal(shown[0][1].body, 'Agent replied');
assert.equal(shown[0][1].tag, 'reply:one');
assert.match(shown[0][1].data.url, /identity=exact/);

listeners.notificationclick({notification: {data: shown[0][1].data, close: () => {}},
  waitUntil: value => { pending = value; }});
await pending;
assert.equal(navigated[0], 'https://desk.example.test/?notification=reply%3Aone&tab=%401&pane=%252&identity=exact');
assert.equal(focused.length, 1);

console.log('service worker tests passed');
