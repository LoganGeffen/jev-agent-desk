const terminalPalette = [
  '#000000', '#cd0000', '#00cd00', '#cdcd00', '#0000ee', '#cd00cd', '#00cdcd', '#e5e5e5',
  '#7f7f7f', '#ff0000', '#00ff00', '#ffff00', '#5c5cff', '#ff00ff', '#00ffff', '#ffffff'
];

class ConversationHistory {
  constructor(element, post) {
    this.element = element;
    this.post = post;
    this.turns = new Map();
    this.status = document.createElement('div');
    this.status.className = 'history-status';
    element.append(this.status);
    element.addEventListener('touchstart', () => { this.touching = true; }, {passive: true});
    for (const name of ['touchend', 'touchcancel']) {
      element.addEventListener(name, () => { this.touching = false; }, {passive: true});
    }
    element.addEventListener('scroll', () => {
      if (element.scrollTop < 1200 && this.before) this.load(true);
    }, {passive: true});
  }
  update(target, available) {
    if (!available) { this.status.textContent = 'Waiting for conversation identity…'; return; }
    if (target.identity !== this.identity) {
      this.identity = target.identity;
      this.target = target;
      this.turns.clear();
      this.element.replaceChildren(this.status);
      this.before = null;
      this.loaded = false;
      this.loading = null;
      this.lastFetch = 0;
    }
    if (Date.now() - this.lastFetch > 3000) this.load(false);
  }
  async load(older) {
    if (this.loading || !this.target || this.element.hidden || !this.element.isConnected) return;
    const target = this.target;
    const request = {};
    this.loading = request;
    this.lastFetch = Date.now();
    this.status.textContent = older ? 'Loading earlier messages…' : this.loaded ? '' : 'Loading conversation…';
    try {
      const result = await this.post('/api/history', {...target, ...(older ? {before: this.before} : {})});
      if (this.target !== target || !this.element.isConnected || result.identity !== target.identity) return;
      const element = this.element;
      const first = !this.loaded;
      const following = !this.touching && element.scrollHeight - element.scrollTop - element.clientHeight < 24;
      const anchor = [...element.querySelectorAll('.history-turn')].find(node => node.getBoundingClientRect().bottom > element.getBoundingClientRect().top);
      const anchorTop = anchor?.getBoundingClientRect().top;
      const insertion = older ? this.status.nextSibling : null;
      for (const turn of result.turns) {
        let node = this.turns.get(turn.id);
        if (!node) {
          node = document.createElement('article');
          node.className = 'history-turn';
          this.turns.set(turn.id, node);
          element.insertBefore(node, insertion);
        }
        this.renderTurn(node, turn);
      }
      if (first || older) this.before = result.before;
      this.loaded = true;
      this.status.textContent = result.missing ? 'No saved messages yet.' : this.before ? '' : 'Start of conversation';
      if (first || (!older && following)) element.scrollTop = element.scrollHeight;
      else if (anchor) element.scrollTop += anchor.getBoundingClientRect().top - anchorTop;
    } catch (error) {
      if (this.target === target) this.status.textContent = 'History unavailable: ' + error.message;
    } finally {
      if (this.loading === request) this.loading = null;
    }
  }
  renderTurn(node, turn) {
    const items = (turn.items || []).filter(item => !['reasoning', 'thinking', 'redacted_thinking'].includes(item.type));
    items.forEach((item, index) => {
      const encoded = JSON.stringify(item);
      let message = node.querySelectorAll('.history-message')[index];
      if (message?.encoded === encoded) return;
      if (!message) { message = document.createElement('div'); node.append(message); }
      message.encoded = encoded;
      message.className = 'history-message';
      if (item.type === 'userMessage') {
        message.classList.add('user');
        message.textContent = (item.content || []).map(part => part.type === 'text' ? part.text
          : '[' + [part.type, part.name || part.filename || part.fileName || part.path, part.mimeType || part.media_type].filter(Boolean).join(' · ') + ']\n' + (part.availability || 'Attachment metadata only.')).join('\n');
      } else if (item.type === 'agentMessage') {
        message.innerHTML = DOMPurify.sanitize(marked.parse(item.text || ''), {
          ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'del', 'blockquote', 'ul', 'ol', 'li', 'pre', 'code', 'h1', 'h2', 'h3', 'h4', 'hr', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a'],
          ALLOWED_ATTR: ['href', 'title', 'start']});
        for (const link of message.querySelectorAll('a')) { link.target = '_blank'; link.rel = 'noopener noreferrer'; }
      } else {
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = [item.tool || item.type, item.status].filter(Boolean).join(' · ');
        details.append(summary);
        details.addEventListener('toggle', () => {
          if (!details.open || details.children.length > 1) return;
          const body = document.createElement('pre');
          body.textContent = item.preview || JSON.stringify(item, null, 2);
          details.append(body);
          if (item.shortened && item.detail_ref) this.addDetails(details, body, item);
          else if (item.shortened) body.textContent += '\n\nOutput shortened; refresh history for full details.';
        });
        message.replaceChildren(details);
      }
    });
    const messages = [...node.querySelectorAll('.history-message')];
    for (const message of messages.slice(items.length)) message.remove();
    let status = node.querySelector('.history-turn-status');
    if (!status) { status = document.createElement('small'); status.className = 'history-turn-status'; node.append(status); }
    const statusText = turn.status ? 'Turn: ' + turn.status : '';
    if (status.textContent !== statusText) status.textContent = statusText;
    if (node.lastChild !== status) node.append(status);
  }
  addDetails(details, body, item) {
    const target = this.target;
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = 'Load full output';
    const note = document.createElement('small');
    let offset = 0;
    const current = () => this.target === target && details.isConnected;
    button.addEventListener('click', async () => {
      if (button.disabled || !current()) return;
      button.disabled = true;
      note.textContent = ' Loading…';
      try {
        const result = await this.post('/api/history/details', {...target, ...item.detail_ref, offset});
        if (!current() || result.identity !== target.identity || result.item_key !== item.detail_ref.item_key || result.offset !== offset) return;
        const top = this.element.scrollTop;
        const anchor = details.firstChild.getBoundingClientRect().top;
        if (offset === 0) body.textContent = '';
        body.append(document.createTextNode(result.text));
        offset = result.nextOffset;
        note.textContent = offset === null ? ' Complete output.' : ' Loaded ' + offset + ' of ' + result.total + ' characters.';
        button.textContent = offset === null ? 'Full output loaded' : 'Load more output';
        this.element.scrollTop = top + details.firstChild.getBoundingClientRect().top - anchor;
      } catch (error) {
        if (current()) note.textContent = ' Details unavailable: ' + error.message;
      } finally {
        if (current()) button.disabled = offset === null;
      }
    });
    details.append(button, note);
  }
}

function terminalColor(index) {
  if (index < 16) return terminalPalette[index];
  if (index >= 232) {
    const gray = 8 + (index - 232) * 10;
    return `rgb(${gray}, ${gray}, ${gray})`;
  }
  const levels = [0, 95, 135, 175, 215, 255];
  const value = index - 16;
  return `rgb(${levels[Math.floor(value / 36)]}, ${levels[Math.floor(value / 6) % 6]}, ${levels[value % 6]})`;
}

function terminalRuns(screen) {
  const runs = [];
  let style = {};
  const parts = screen.split(/(\x1b\[[0-9;:]*m)/);
  for (const part of parts) {
    if (!part.startsWith('\x1b[')) {
      if (part) runs.push({text: part, ...style});
      continue;
    }
    const codes = part.slice(2, -1).split(/[;:]/).map(Number);
    for (let i = 0; i < codes.length; i++) {
      const code = codes[i];
      if (code === 0) style = {};
      else if (code === 1) style.bold = true;
      else if (code === 2) style.dim = true;
      else if (code === 3) style.italic = true;
      else if (code === 4) style.underline = true;
      else if (code === 7) style.inverse = true;
      else if (code === 9) style.strike = true;
      else if (code === 22) { delete style.bold; delete style.dim; }
      else if (code === 23) delete style.italic;
      else if (code === 24) delete style.underline;
      else if (code === 27) delete style.inverse;
      else if (code === 29) delete style.strike;
      else if (code === 39) delete style.foreground;
      else if (code === 49) delete style.background;
      else if (code >= 30 && code <= 37) style.foreground = terminalColor(code - 30);
      else if (code >= 40 && code <= 47) style.background = terminalColor(code - 40);
      else if (code >= 90 && code <= 97) style.foreground = terminalColor(code - 90 + 8);
      else if (code >= 100 && code <= 107) style.background = terminalColor(code - 100 + 8);
      else if (code === 38 || code === 48) {
        const property = code === 38 ? 'foreground' : 'background';
        const mode = codes[++i];
        if (mode === 5) style[property] = terminalColor(codes[++i]);
        else if (mode === 2) {
          style[property] = `rgb(${codes[i + 1]}, ${codes[i + 2]}, ${codes[i + 3]})`;
          i += 3;
        }
      }
    }
  }
  return runs;
}

function renderTerminal(element, screen) {
  if (element.terminalScreen === screen) return false;
  const top = element.scrollTop, left = element.scrollLeft;
  const following = element.scrollHeight - top - element.clientHeight <= 1;
  element.terminalScreen = screen;
  const fragment = document.createDocumentFragment();
  for (const run of terminalRuns(screen)) {
    const span = document.createElement('span');
    span.textContent = run.text;
    const foreground = run.foreground || 'var(--terminal-foreground)';
    const background = run.background || 'var(--terminal-background)';
    span.style.color = run.inverse ? background : foreground;
    span.style.backgroundColor = run.inverse ? foreground : background;
    if (run.inverse) span.classList.add('terminal-inverse');
    if (run.bold) span.style.fontWeight = 'bold';
    if (run.dim) span.style.opacity = '.65';
    if (run.italic) span.style.fontStyle = 'italic';
    span.style.textDecoration = [run.underline && 'underline', run.strike && 'line-through'].filter(Boolean).join(' ');
    fragment.append(span);
  }
  element.replaceChildren(fragment);
  element.scrollTop = following ? element.scrollHeight : top;
  element.scrollLeft = left;
  return true;
}

function bindTerminalScroll(element, send, onError, trace = () => {}) {
  let touch = null, pending = false, wheel = 0;
  const report = (event, extra = {}) => {
    try { trace({event, remote: element.remoteScroll, top: element.scrollTop,
      height: element.clientHeight, contentHeight: element.scrollHeight, ...extra}); } catch {}
  };
  const atEdge = delta => element.remoteScroll && (delta < 0
    ? element.scrollTop <= 1
    : element.scrollHeight - element.scrollTop - element.clientHeight <= 1);
  const target = () => ({tab: element.dataset.tab, pane: element.dataset.pane, identity: element.dataset.identity});
  const page = (delta, captured) => {
    if (pending) return;
    pending = true;
    report('page_requested', {target: captured, direction: delta < 0 ? 'up' : 'down'});
    Promise.resolve().then(() => send({...captured, direction: delta < 0 ? 'up' : 'down'}))
      .then(() => report('page_submitted'))
      .catch(error => { report('page_failed', {error: String(error)}); onError(error); })
      .finally(() => { pending = false; });
  };
  element.addEventListener('wheel', event => {
    if (event.ctrlKey || !atEdge(event.deltaY)) { wheel = 0; return; }
    event.preventDefault();
    wheel = Math.sign(wheel) === Math.sign(event.deltaY) ? wheel + event.deltaY : event.deltaY;
    if (Math.abs(wheel) >= 60) { page(wheel, target()); wheel = 0; }
  }, {passive: false});
  element.addEventListener('touchstart', event => {
    touch = event.touches.length === 1 ? {y: event.touches[0].clientY, target: target()} : null;
    report('touchstart', {target: target(), touches: event.touches.length});
  }, {passive: true});
  element.addEventListener('touchmove', event => {
    if (!touch || event.touches.length !== 1 || touch.target.identity !== element.dataset.identity) {
      if (touch) report('touch_invalidated', {identityChanged: touch.target.identity !== element.dataset.identity});
      touch = null;
      return;
    }
    const y = event.touches[0].clientY;
    const delta = touch.y - y;
    if (!atEdge(delta)) { touch.y = y; return; }
    event.preventDefault();
    if (Math.abs(delta) >= 60) { page(delta, touch.target); touch.y = y; }
  }, {passive: false});
  for (const name of ['touchend', 'touchcancel']) {
    element.addEventListener(name, () => { report(name); touch = null; }, {passive: true});
  }
}
