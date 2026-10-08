import assert from 'node:assert/strict';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const {PushNotificationClient} = require('./notifications.js');

class Storage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, value); }
}

const publicKey = Buffer.concat([Buffer.from([4]), Buffer.alloc(64, 7)]).toString('base64url');

function environment({search = '', open = true} = {}) {
  const calls = [];
  let subscribed = null;
  const pushManager = {
    getSubscription: async () => subscribed,
    subscribe: async options => {
      assert.equal(options.userVisibleOnly, true);
      assert.equal(options.applicationServerKey.length, 65);
      subscribed = {toJSON: () => ({endpoint: 'https://fcm.googleapis.com/fcm/send/device', expirationTime: null,
        keys: {auth: 'auth', p256dh: 'key'}}), unsubscribe: async () => { subscribed = null; return true; }};
      return subscribed;
    }
  };
  const registration = {pushManager};
  const responses = {
    '/api/push/state': {public_key: publicKey, enabled: false, muted: false},
    '/api/push/subscribe': {enabled: true},
    '/api/push/test': {queued: true, delay_seconds: 2},
    '/api/push/mute': {muted: true},
    '/api/push/unsubscribe': {enabled: false},
    '/api/push/open': {status: open ? 'opened' : 'stale'},
  };
  return {calls, storage: new Storage(), serviceWorker: {register: async path => {
    assert.equal(path, '/service-worker.js');
    return registration;
  }},
  NotificationApi: {requestPermission: async () => 'granted'},
  location: {search, pathname: '/', origin: 'https://desk.example.test', href: 'https://desk.example.test/'},
  history: {paths: [], replaceState(_a, _b, path) { this.paths.push(path); }},
  fetch: async (path, options) => {
    calls.push({path, body: JSON.parse(options.body)});
    return {ok: true, json: async () => responses[path]};
  }};
}

{
  const env = environment();
  const views = [];
  const client = new PushNotificationClient({...env, onChange: view => views.push(view)});
  const target = {tab: '@1', pane: '%2', identity: 'process:codex:thread'};
  await client.adopt('session', target);
  await client.enable();
  await client.test();
  await client.setMuted(true);
  await client.disable();
  assert.deepEqual(env.calls.map(call => call.path), [
    '/api/push/state', '/api/push/subscribe', '/api/push/test', '/api/push/mute', '/api/push/unsubscribe'
  ]);
  assert.deepEqual(Object.keys(env.calls[1].body.subscription).sort(), ['endpoint', 'keys']);
  assert.deepEqual(env.calls[2].body.target, target);
  assert.equal(views.at(-1).enabled, false);
  assert.match(views.at(-1).message, /off on this device/);
}

{
  const event = 'reply:' + 'a'.repeat(64);
  const target = {tab: '@1', pane: '%2', identity: 'process:codex:thread'};
  const search = '?' + new URLSearchParams({notification: event, ...target});
  const env = environment({search});
  const client = new PushNotificationClient({...env, onChange: () => {}});
  await client.adopt('session', target);
  const navigated = [];
  await client.resolveLink({tabs: [{id: '@1', panes: [{id: '%2', identity: target.identity}]}]},
    async (tab, pane) => navigated.push([tab, pane]));
  assert.deepEqual(navigated, [['@1', '%2']]);
  assert.equal(env.calls.at(-1).body.status, 'opened');
  assert.equal(env.calls.at(-1).body.event_id, event);
  assert.deepEqual(env.history.paths, ['/']);
  assert.match(client.view.linkMessage, /exact conversation/);
}

{
  const event = 'reply:' + 'b'.repeat(64);
  const target = {tab: '@1', pane: '%2', identity: 'old:codex:thread'};
  const env = environment({search: '?' + new URLSearchParams({notification: event, ...target}), open: false});
  const client = new PushNotificationClient({...env, onChange: () => {}});
  await client.adopt('session', target);
  let navigated = false;
  await client.resolveLink({tabs: [{id: '@1', panes: [{id: '%2', identity: 'replacement:codex:thread'}]}]},
    async () => { navigated = true; });
  assert.equal(navigated, false);
  assert.equal(env.calls.at(-1).body.status, 'stale');
  assert.match(client.view.linkMessage, /No other conversation was opened/);
}

{
  const env = environment();
  let stateRequests = 0;
  env.fetch = async (path, options) => {
    env.calls.push({path, body: JSON.parse(options.body)});
    if (path === '/api/push/state') {
      stateRequests += 1;
      return {ok: true, json: async () => ({public_key: publicKey, enabled: stateRequests === 1, muted: false})};
    }
    assert.equal(path, '/api/push/test');
    return {ok: false, json: async () => ({error: 'Notifications are not enabled on this device'})};
  };
  const client = new PushNotificationClient({...env, onChange: () => {}});
  await client.adopt('session', {tab: '@1', pane: '%2', identity: 'process:codex:thread'});
  assert.equal(client.view.enabled, true);
  await client.test();
  assert.equal(stateRequests, 2);
  assert.equal(client.view.enabled, false);
  assert.equal(client.view.message, 'Notifications are not enabled on this device');
}

console.log('notification client tests passed');
