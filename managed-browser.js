class ManagedBrowserView {
  constructor(root, {clientId, activity, preference, save}) {
    Object.assign(this, {root, clientId, activity, preference, save});
    this.epoch = 0;
    this.pages = [];
    this.queue = Promise.resolve();
    this.root.innerHTML = `<div class="browser-controls"><select class="browser-pages" aria-label="Browser page"></select><span class="browser-account"></span><button class="browser-signin" type="button">Sign in</button><button class="browser-done" type="button" hidden>Done</button></div><p class="browser-status" role="status"></p><form class="browser-address" hidden><input type="url" aria-label="Browser address during sign-in" placeholder="https://" autocomplete="off"><button>Go</button></form><div class="browser-stage"><canvas tabindex="0" aria-label="Conversation browser" hidden></canvas><p class="visual-message">Ask this conversation to use its managed browser. Its page will appear here.</p></div><div class="browser-tools"><label>Local zoom <select class="browser-zoom"><option value="fit">Fit</option><option value="1">100%</option><option value="1.5">150%</option><option value="2">200%</option></select></label><button class="browser-reconnect" type="button">Reconnect view</button><button class="browser-keyboard" type="button" hidden>Keyboard</button><button class="browser-enter" type="button" hidden>Enter</button><button class="browser-tab" type="button" hidden>Tab</button><input class="browser-typing" type="password" aria-label="Type directly into controlled browser" autocomplete="off" autocapitalize="off" spellcheck="false" tabindex="-1"></div><p class="browser-control-note" hidden></p><details class="browser-setup"><summary>Account environment</summary><p>Choose a separate Desk account. Personal browser cookies are never imported.</p><form><label>Account label<input class="browser-profile" value="Desk account" maxlength="60"></label><button>Open browser</button></form></details>`;
    this.find = selector => root.querySelector(selector);
    this.canvas = this.find('canvas');
    this.find('.browser-pages').onchange = event => {
      const previous = this.page();
      this.preference().browserPage = event.target.value;
      const page = this.page();
      if (this.lease && previous?.profile_id === page?.profile_id && previous.run_id === page.run_id && this.leasePage(page)) this.lease = {...this.lease, target: page.target, page_id: page.page_id};
      else this.disconnectHuman();
      this.save(); this.connect(true); this.controls();
    };
    this.find('.browser-zoom').onchange = event => { this.preference().browserZoom = event.target.value; this.save(); this.zoom(); };
    this.find('.browser-signin').onclick = () => this.signIn();
    this.find('.browser-done').onclick = () => this.done();
    this.find('.browser-address').onsubmit = event => { event.preventDefault(); const url = this.find('.browser-address input').value.trim(); if (/^https?:\/\//i.test(url)) this.input('navigate', {url}); else this.status('Enter an http or https address.'); };
    this.find('.browser-reconnect').onclick = () => { this.failed = null; this.refresh(true); };
    this.find('.browser-keyboard').onclick = () => this.find('.browser-typing').focus();
    this.find('.browser-enter').onclick = () => this.input('key', {key: 'Enter'});
    this.find('.browser-tab').onclick = () => this.input('key', {key: 'Tab'});
    this.find('.browser-typing').addEventListener('input', event => {
      if (event.isComposing) return;
      const text = event.target.value;
      event.target.value = '';
      if (text) this.input('text', {text});
    });
    this.find('.browser-typing').addEventListener('compositionend', event => { const text = event.target.value; event.target.value = ''; if (text) this.input('text', {text}); });
    const key = event => {
      if (!this.lease || event.isComposing) return;
      if (['Enter', 'Tab', 'Backspace', 'Delete', 'Escape', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) {
        event.preventDefault(); this.input('key', {key: event.shiftKey && event.key === 'Tab' ? 'Shift+Tab' : event.key});
      } else if (event.target === this.canvas && event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault(); this.input('text', {text: event.key});
      }
    };
    this.canvas.addEventListener('keydown', key);
    this.find('.browser-typing').addEventListener('keydown', key);
    this.canvas.addEventListener('pointerdown', event => {
      if (!this.lease) return;
      this.pointer = {x: event.clientX, y: event.clientY};
      this.canvas.setPointerCapture(event.pointerId);
    });
    this.canvas.addEventListener('pointerup', event => {
      if (!this.lease || !this.pointer) return;
      const start = this.pointer; this.pointer = null;
      const delta = {x: start.x - event.clientX, y: start.y - event.clientY};
      if (Math.hypot(delta.x, delta.y) > 10) this.input('scroll', {x: Math.round(delta.x * 3), y: Math.round(delta.y * 3)});
      else { const rect = this.canvas.getBoundingClientRect(); this.input('click', {x: (event.clientX - rect.left) / rect.width * this.canvas.width, y: (event.clientY - rect.top) / rect.height * this.canvas.height}); }
    });
    this.canvas.addEventListener('wheel', event => { if (!this.lease) return; event.preventDefault(); this.input('scroll', {x: event.deltaX, y: event.deltaY}); }, {passive: false});
    this.find('.browser-stage').onscroll = event => { Object.assign(this.preference(), {browserScrollX: event.target.scrollLeft, browserScrollY: event.target.scrollTop}); this.save(); };
    this.find('.browser-setup form').onsubmit = async event => {
      event.preventDefault();
      const label = this.find('.browser-profile').value.trim();
      if (!label || !this.context) return;
      const profile = this.profiles?.find(item => item.account_label === label)?.profile_id || (label === 'Desk account' ? 'desk' : 'account-' + crypto.randomUUID());
      try { await this.post('open', {target: this.target(), profile_id: profile, account_label: label}); await this.refresh(true); }
      catch (error) { this.status(error.message); }
    };
    addEventListener('pagehide', () => this.disconnectHuman(true));
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { this.disconnectHuman(true); this.stop(); }
      else { this.failed = null; this.refresh(true); }
    });
  }

  target() { return {tab: this.context.tab, pane: this.context.pane, identity: this.context.identity}; }

  update(context) {
    if (this.context?.key !== context?.key) {
      this.disconnectHuman(); this.stop(); this.epoch += 1;
      this.context = context; this.pages = []; this.lastRefresh = 0; this.failed = null;
      this.find('.browser-pages').replaceChildren(); this.find('.browser-account').textContent = ''; this.status('Loading the selected conversation’s browser…');
    }
    this.refresh();
  }

  show(visible) {
    this.visible = visible;
    this.root.hidden = !visible;
    if (!visible) { this.disconnectHuman(); this.stop(); }
    else this.connect();
  }

  async refresh(force = false) {
    if (!this.context || this.loading || (!force && Date.now() - this.lastRefresh < 1500)) return;
    const epoch = this.epoch, context = this.context;
    this.loading = true; this.lastRefresh = Date.now();
    try {
      const response = await fetch('/api/browser/state?' + new URLSearchParams({tab: context.tab}));
      const state = await response.json();
      if (epoch !== this.epoch) return;
      if (!response.ok) throw new Error(state.error || 'Managed browser unavailable.');
      const pages = state.pages.filter(page => page.target?.pane === context.pane && page.target.identity === context.identity);
      const before = this.pages.map(page => page.page_id + page.run_id).join();
      this.pages = pages; this.profiles = state.profiles;
      const choice = this.preference();
      if (!pages.some(page => page.page_id === choice.browserPage)) choice.browserPage = pages.find(page => !['closed', 'stale'].includes(page.status))?.page_id || pages[0]?.page_id;
      const signature = JSON.stringify(pages.map(page => [page.page_id, page.run_id, page.title, page.url]));
      if (signature !== this.optionSignature) {
        this.optionSignature = signature;
        this.find('.browser-pages').replaceChildren(...pages.map(page => new Option((page.parent_page_id ? 'Popup · ' : '') + (page.title || page.url || 'Browser page'), page.page_id)));
      }
      if (pages.map(page => page.page_id + page.run_id).join() !== before) {
        if (pages.some(page => !['closed', 'stale'].includes(page.status))) this.activity(pages.map(page => page.page_id + page.run_id).join(), 'live');
      }
      this.find('.browser-pages').value = choice.browserPage || '';
      const page = this.page();
      this.find('.browser-account').textContent = page?.account_label || 'No browser page';
      if (!this.lease && page) this.status(({human: 'Waiting while another device signs in. Automation is paused for this account.', waiting: 'Sign-in paused. Choose Sign in to continue, then Done.', pausing: 'Waiting for browser calls to finish before sign-in.', stale: 'Browser restarted. Ask the conversation to reopen its page.', closed: 'This browser page is closed.'})[page.status] || 'Agent browser · view only');
      this.controls(); this.connect();
    } catch (error) { if (epoch === this.epoch) { this.stop(); this.status(error.message); } }
    finally { if (epoch === this.epoch) this.loading = false; else { this.loading = false; this.refresh(true); } }
  }

  page() { return this.pages.find(page => page.page_id === this.preference().browserPage); }

  status(text) { this.find('.browser-status').textContent = text; this.find('.browser-status').hidden = text === 'Agent browser · view only'; this.find('.visual-message').textContent = text; }

  connect(replace = false) {
    if (!this.visible || document.hidden) return;
    const page = this.page();
    if (!page || ['closed', 'stale'].includes(page.status)) { this.stop(); return; }
    const key = JSON.stringify([this.context.key, page.page_id, page.run_id]);
    if (!replace && (key === this.streamKey || key === this.failed)) return;
    this.stop(); this.streamKey = key;
    const epoch = this.epoch;
    const url = new URL(page.stream_url, location.origin);
    if (url.origin !== location.origin || url.pathname !== '/api/browser/stream') return this.status('Invalid browser stream address.');
    url.searchParams.set('client_id', this.clientId);
    const stream = this.stream = new EventSource(url.href);
    let sequence = 0;
    const current = () => this.stream === stream && this.epoch === epoch && this.streamKey === key;
    stream.addEventListener('frame', event => {
      if (!current()) return;
      let frame; try { frame = JSON.parse(event.data); } catch { return; }
      if (frame.page_id !== page.page_id || frame.run_id !== page.run_id || frame.sequence <= sequence || !['image/png', 'image/jpeg'].includes(frame.mime) || typeof frame.data !== 'string' || frame.data.length > 16 * 1024 * 1024) return;
      const image = new Image();
      image.onload = () => {
        if (!current() || frame.sequence <= sequence) return;
        const first = sequence === 0;
        sequence = frame.sequence;
        this.canvas.width = image.naturalWidth; this.canvas.height = image.naturalHeight;
        this.canvas.getContext('2d').drawImage(image, 0, 0);
        this.canvas.hidden = false; this.find('.visual-message').hidden = true; this.zoom();
        if (first) { const stage = this.find('.browser-stage'); stage.scrollLeft = this.preference().browserScrollX || 0; stage.scrollTop = this.preference().browserScrollY || 0; }
      };
      image.src = `data:${frame.mime};base64,${frame.data}`;
    });
    stream.addEventListener('status', event => { if (!current()) return; let state; try { state = JSON.parse(event.data); } catch { return; } if (state.page_id === page.page_id && state.run_id === page.run_id) { this.failed = key; this.stop(); this.disconnectHuman(); this.status(state.error || 'Browser view unavailable.'); } });
    stream.onerror = () => { if (current()) { this.failed = key; this.stop(); this.disconnectHuman(); this.status('Browser disconnected. Reconnect the view when the host is available.'); } };
  }

  zoom() {
    const zoom = this.preference().browserZoom || 'fit';
    this.find('.browser-zoom').value = zoom;
    this.canvas.style.width = zoom === 'fit' ? '100%' : `${this.canvas.width * Number(zoom)}px`;
    this.canvas.style.maxWidth = zoom === 'fit' ? '100%' : 'none';
    this.canvas.style.height = 'auto';
  }

  stop() {
    this.stream?.close(); this.stream = null; this.streamKey = null;
    this.canvas.hidden = true; this.canvas.width = 0; this.find('.visual-message').hidden = false;
  }

  controls() {
    const controlled = !!this.lease;
    for (const selector of ['.browser-done', '.browser-keyboard', '.browser-enter', '.browser-tab', '.browser-address']) this.find(selector).hidden = !controlled;
    const waiting = this.profiles?.find(profile => profile.profile_id === this.page()?.profile_id)?.status === 'waiting';
    this.find('.browser-signin').hidden = controlled || !this.page() || ['closed', 'human', 'pausing'].includes(this.page()?.status) || (this.page()?.status === 'stale' && !waiting);
    this.canvas.style.touchAction = controlled ? 'none' : 'auto';
    this.find('.browser-control-note').hidden = !controlled;
    if (controlled) this.find('.browser-control-note').textContent = 'You control this page. Every conversation sharing this account is paused. Done returns control; it does not verify sign-in.';
  }

  async signIn() {
    const page = this.page(), epoch = this.epoch;
    if (!page || this.lease) return;
    this.status('Pausing all conversations sharing this account…');
    try {
      const lease = await this.post('lease', {target: this.target(), page_id: page.page_id, run_id: page.run_id, client_id: this.clientId});
      if (epoch !== this.epoch || this.page()?.page_id !== page.page_id || !this.visible) {
        await this.post('disconnect', {target: lease.target || page.target, page_id: lease.page_id, run_id: lease.run_id, client_id: this.clientId, lease_id: lease.lease_id}); return;
      }
      if (lease.page_id !== page.page_id || lease.run_id !== page.run_id) {
        this.pages = this.pages.filter(item => item.page_id !== page.page_id); this.pages.push(lease);
        this.preference().browserPage = lease.page_id; this.save(); this.failed = null; this.connect(true);
      }
      this.lease = {target: lease.target || page.target, page_id: lease.page_id, run_id: lease.run_id, client_id: this.clientId, lease_id: lease.lease_id};
      this.find('.browser-address input').value = lease.url?.startsWith('http') ? lease.url : '';
      this.leaseRootPage = lease.page_id;
      this.sequence = 0; this.controls(); this.status(lease.message || 'Sign in directly in the browser, then choose Done.');
      this.heartbeat = setInterval(() => this.renew(), 6000);
    } catch (error) { if (epoch === this.epoch) { this.status(error.message); this.refresh(true); } }
  }

  leasePage(page) {
    for (let count = 0; page && count < 12; count += 1) {
      if (page.page_id === this.leaseRootPage) return true;
      page = this.pages.find(item => item.page_id === page.parent_page_id);
    }
    return false;
  }

  async renew() {
    const lease = this.lease;
    if (!lease) return;
    try { await this.post('heartbeat', lease); }
    catch { if (lease === this.lease) { this.disconnectHuman(); this.status('Control was lost. Automation stays paused. Choose Sign in to continue.'); } }
  }

  input(action, args) {
    const lease = this.lease;
    if (!lease) return;
    const sequence = ++this.sequence;
    this.queue = this.queue.then(async () => {
      if (this.lease !== lease) return;
      try { await this.post('input', {...lease, sequence, action, args}); }
      catch { if (this.lease === lease) { this.disconnectHuman(); this.status('Input outcome is uncertain. It will not be repeated. Review the browser before continuing.'); } }
    });
  }

  async done() {
    const lease = this.lease;
    if (!lease) return;
    await this.queue;
    if (this.lease !== lease) return;
    try {
      const result = await this.post('done', lease);
      if (this.lease !== lease) return;
      this.lease = null; clearInterval(this.heartbeat); this.controls();
      this.status(result.message || 'Control returned. The agent must check the current account state.'); this.refresh(true);
    } catch (error) { if (this.lease === lease) { this.disconnectHuman(); this.status(error.message + ' Automation stays paused until you take control again.'); } }
  }

  disconnectHuman(beacon = false) {
    const lease = this.lease;
    this.lease = null; clearInterval(this.heartbeat); this.find('.browser-typing').value = ''; this.controls();
    if (!lease) return;
    if (beacon) navigator.sendBeacon('/api/browser/disconnect', new Blob([JSON.stringify(lease)], {type: 'application/json'}));
    else this.post('disconnect', lease).catch(() => {});
  }

  async post(operation, body) {
    const response = await fetch('/api/browser/' + operation, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Browser request failed.');
    return result;
  }
}
