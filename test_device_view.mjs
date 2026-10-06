import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function setup(saved = null) {
  const values = new Map(saved ? [['jev-device-view', JSON.stringify(saved)]] : []);
  const storage = {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value)};
  const context = vm.createContext({localStorage: storage, location: {search: ''}, crypto: {randomUUID: () => 'device'}, URLSearchParams});
  vm.runInContext(readFileSync(new URL('./device-view.js', import.meta.url), 'utf8') + '\nthis.view = new DeviceView();', context);
  return {view: context.view, storage};
}

function state(selected = '@a', identity = 'a:original') {
  return {selected, selected_pane: selected.replace('@', '%'), tabs: ['a', 'b'].map(letter => ({id: '@' + letter,
    workspace_id: 'workspace-' + letter, panes: [{id: '%' + letter, identity: letter === 'a' ? identity : 'b:original', active: true,
      ...(selected === '@' + letter ? {terminal_ansi: letter + ' terminal'} : {})}]}))};
}

test('independent devices preserve their own selection despite opposite server snapshots', () => {
  const desktop = setup().view, phone = setup().view;
  desktop.adopt(state()); phone.adopt(state());
  phone.select(state(), '@b');
  assert.equal(desktop.adopt(state('@b')).selected, '@a');
  assert.equal(phone.adopt(state('@a')).selected, '@b');
  assert.equal(desktop.query, '?tab=%40a&pane=%25a');
  assert.equal(phone.query, '?tab=%40b&pane=%25b');
});

test('reload keeps workspace selection and cannot inherit recycled tmux id', () => {
  const {view, storage} = setup();
  view.adopt(state()); view.select(state(), '@b');
  const restored = setup(JSON.parse(storage.getItem('jev-device-view'))).view;
  assert.equal(restored.adopt(state()).selected, '@b');
  const replaced = state(); replaced.tabs[1].workspace_id = 'different-incarnation';
  assert.equal(restored.adopt(replaced).selected, '@a');
});

test('uncaptured terminal snapshots keep cached text only for identical conversation identity', () => {
  const {view} = setup();
  view.adopt(state());
  let next = view.adopt(state('@b'));
  assert.equal(next.tabs[0].panes[0].terminal_ansi, 'a terminal');
  next = view.adopt(state('@b', 'a:replaced'));
  assert.equal(next.tabs[0].panes[0].terminal_ansi, '');
});
