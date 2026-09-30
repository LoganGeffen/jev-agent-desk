import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function setup() {
  const document = {
    createDocumentFragment: () => ({children: [], append(node) { this.children.push(node); }}),
    createElement: () => ({style: {}, classList: new Set()})
  };
  const context = vm.createContext({document});
  vm.runInContext(readFileSync(new URL('./terminal.js', import.meta.url), 'utf8'), context);
  const terminal = {replaceChildren(fragment) { this.children = fragment.children; }};
  return {context, terminal};
}

test('Codex transcript selection remains visible when only the inverse style changes', () => {
  const {context, terminal} = setup();
  context.renderTerminal(terminal, '› First\n› \x1b[7mSecond\x1b[0m');
  assert.equal(terminal.children.find(node => node.classList.has('terminal-inverse')).textContent, 'Second');
  context.renderTerminal(terminal, '› \x1b[7mFirst\x1b[0m\n› Second');
  const selected = terminal.children.find(node => node.classList.has('terminal-inverse'));
  assert.equal(selected.textContent, 'First');
  assert.equal(selected.style.backgroundColor, 'var(--terminal-foreground)');
  assert.equal(selected.style.color, 'var(--terminal-background)');
  assert.equal(terminal.children.map(node => node.textContent).join(''), '› First\n› Second');
});

test('colour, inverse and reset survive tmux SGR captures', () => {
  const {context, terminal} = setup();
  context.renderTerminal(terminal, '\x1b[38;5;196;48;2;10;20;30;1;7mselected\x1b[0mnormal');
  assert.equal(terminal.children[0].style.color, 'rgb(10, 20, 30)');
  assert.equal(terminal.children[0].style.backgroundColor, 'rgb(255, 0, 0)');
  assert.equal(terminal.children[0].style.fontWeight, 'bold');
  assert.equal(terminal.children[1].style.color, 'var(--terminal-foreground)');
  assert.equal(terminal.children[1].classList.has('terminal-inverse'), false);
});

test('terminal content is rendered as text, never interpreted as HTML', () => {
  const {context, terminal} = setup();
  const text = '<img src=x onerror=alert(1)> & <script>danger()</script>';
  context.renderTerminal(terminal, '\x1b[7m' + text + '\x1b[0m');
  assert.equal(terminal.children[0].textContent, text);
  assert.equal(terminal.children[0].innerHTML, undefined);
});
