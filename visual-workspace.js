class VisualWorkspace {
  constructor(root, toggle) {
    this.root = root;
    this.toggle = toggle;
    this.context = null;
    this.epoch = 0;
    this.previewEpoch = 0;
    this.choices = new Map();
    this.mode = 'creations';
    this.open = !window.matchMedia('(max-width: 700px)').matches;
    this.lastRefresh = 0;
    this.root.innerHTML = `
      <div class="visual-heading"><h2>Visual workspace</h2><span class="visual-owner"></span></div>
      <div class="visual-tabs" role="tablist" aria-label="Visual workspace views">
        <button type="button" id="visual-creations-tab" role="tab" aria-controls="visual-creations" aria-selected="true">Creations<span class="visual-count"></span></button>
        <button type="button" id="visual-live-tab" role="tab" aria-controls="visual-live" aria-selected="false" tabindex="-1">Live browser</button>
      </div>
      <section id="visual-creations" class="visual-panel" role="tabpanel" aria-labelledby="visual-creations-tab">
        <div class="visual-controls" hidden>
          <label>Creation<select class="visual-creation" aria-label="Saved creation"></select></label>
          <label>Version<select class="visual-version" aria-label="Saved version"></select></label>
          <a class="visual-download" download>Download original</a>
        </div>
        <div class="visual-preview"></div>
        <div class="visual-provenance"></div>
      </section>
      <section id="visual-live" class="visual-panel" role="tabpanel" aria-labelledby="visual-live-tab" hidden>
        <p class="visual-status" role="status"></p>
        <div class="visual-live-stage"><canvas aria-label="Live browser view" hidden></canvas><p class="visual-message"></p></div>
        <div class="visual-live-footer"><span>View only · follows the selected pane</span><button type="button" class="visual-retry" hidden>Reconnect view</button></div>
      </section>
      <details class="visual-help"><summary>Save or connect an output</summary>
        <p>From this checkout, save a file or static bundle to the selected conversation:</p><code class="visual-command"></code>
        <p>Repeat with <code style="display:inline">--creation CREATION_ID</code> to save a new version. Connect a named agent-browser session with <code style="display:inline">visual.py attach</code>; see the README for its exact process and port options.</p>
        <p>Only registered outputs appear here. Saved versions keep their original bytes.</p>
      </details>`;
    this.find = selector => this.root.querySelector(selector);
    this.preview = this.find('.visual-preview');
    this.canvas = this.find('canvas');
    this.find('.visual-creation').onchange = event => {
      this.choice().creation = event.target.value;
      this.choice().version = null;
      this.renderCreations();
    };
    this.find('.visual-version').onchange = event => {
      this.choice().version = event.target.value;
      this.renderCreations();
    };
    for (const mode of ['creations', 'live']) {
      const button = this.find(`#visual-${mode}-tab`);
      button.onclick = () => this.setMode(mode);
      button.onkeydown = event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === 'Home' ? 'creations' : event.key === 'End' ? 'live' : mode === 'live' ? 'creations' : 'live';
        this.setMode(next);
        this.find(`#visual-${next}-tab`).focus();
      };
    }
    this.toggle.onclick = () => { this.open = !this.open; this.renderOpen(); };
    this.find('.visual-retry').onclick = () => {
      this.failedLiveKey = null;
      this.lastRefresh = 0;
      this.refresh();
    };
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) this.stopLive();
      else this.renderLive();
    });
    this.renderOpen();
    this.empty('Your tab’s creations', 'Saved files and live browser activity stay with their tab.');
  }

  update(state) {
    const tab = state.tabs.find(item => item.id === state.selected);
    const pane = tab?.panes.find(item => item.id === state.selected_pane);
    const key = JSON.stringify([tab?.id, pane?.id, pane?.identity]);
    if (key !== this.context?.key) {
      this.suspend();
      this.context = tab && pane ? {key, tab: tab.id, pane: pane.id, identity: pane.identity, name: tab.name} : null;
      this.workspace = null;
      this.creationSignature = null;
      this.find('.visual-controls').hidden = true;
      this.find('.visual-count').textContent = '';
      this.find('.visual-provenance').textContent = '';
      this.empty('Loading this tab’s creations…');
    }
    if (!this.context) return;
    this.find('.visual-owner').textContent = tab.name;
    this.find('.visual-command').textContent = `python visual.py --url ${location.origin} register --tab '${tab.id}' --pane '${pane.id}' --title 'My creation' /path/to/output`;
    this.refresh();
  }

  suspend() {
    this.epoch += 1;
    this.context = null;
    this.workspace = null;
    this.request?.abort();
    this.request = null;
    this.lastRefresh = 0;
    this.stopLive();
    this.failedLiveKey = null;
    this.clearPreview();
    this.find('.visual-controls').hidden = true;
    this.find('.visual-provenance').textContent = '';
    this.find('.visual-owner').textContent = '';
    this.find('.visual-count').textContent = '';
    this.liveStatus('connecting', 'Waiting for the selected conversation.');
  }

  offline() {
    this.suspend();
    this.empty('Desk disconnected', 'Reconnect to load this tab’s saved creations.');
    this.liveStatus('disconnected', 'Desk disconnected. The browser view is unavailable.');
  }

  async refresh() {
    if (!this.context || this.request || Date.now() - this.lastRefresh < 2000) return;
    const context = this.context;
    const epoch = this.epoch;
    const request = new AbortController();
    this.request = request;
    this.lastRefresh = Date.now();
    try {
      const response = await fetch('/api/visual/workspace?' + new URLSearchParams({tab: context.tab}), {signal: request.signal});
      const data = await response.json();
      if (epoch !== this.epoch || this.context !== context) return;
      if (!response.ok) throw new Error(data.error || 'Visual workspace unavailable.');
      if (data.tab !== context.tab || !data.workspace_id) throw new Error('The workspace identity could not be verified.');
      if (this.workspace && this.workspace.workspace_id !== data.workspace_id) {
        this.clearPreview();
        this.stopLive();
        this.creationSignature = null;
      }
      this.workspace = data;
      this.find('.visual-count').textContent = String(data.creations.length);
      this.renderCreations();
      this.renderLive();
    } catch (error) {
      if (epoch !== this.epoch || error.name === 'AbortError') return;
      this.workspace = null;
      this.clearPreview();
      this.stopLive();
      this.find('.visual-controls').hidden = true;
      this.find('.visual-provenance').textContent = '';
      this.empty('Visual workspace unavailable', error.message);
      this.liveStatus('disconnected', error.message);
    } finally {
      if (this.request === request) this.request = null;
    }
  }

  renderOpen() {
    this.root.hidden = !this.open;
    this.root.parentElement.classList.toggle('visuals-open', this.open);
    this.toggle.setAttribute('aria-expanded', String(this.open));
    if (this.open) { this.refresh(); this.renderLive(); }
    else { this.stopLive(); this.clearPreview(); }
    if (this.open && this.workspace) this.renderCreations();
  }

  setMode(mode) {
    this.mode = mode;
    for (const name of ['creations', 'live']) {
      this.find(`#visual-${name}`).hidden = name !== mode;
      const button = this.find(`#visual-${name}-tab`);
      button.setAttribute('aria-selected', String(name === mode));
      button.tabIndex = name === mode ? 0 : -1;
    }
    if (mode === 'live') { this.clearPreview(); this.renderLive(); }
    else { this.stopLive(); this.renderCreations(); }
  }

  choice() {
    const id = this.workspace.workspace_id;
    if (!this.choices.has(id)) this.choices.set(id, {});
    return this.choices.get(id);
  }

  renderCreations() {
    if (!this.workspace || !this.open || this.mode !== 'creations') return;
    const creations = this.workspace.creations.filter(item => item.versions.length);
    if (!creations.length) {
      this.clearPreview();
      this.find('.visual-controls').hidden = true;
      this.find('.visual-provenance').textContent = '';
      this.empty('No creations saved yet', 'Save an output from this tab to keep a copy and revisit each version.');
      return;
    }
    const choice = this.choice();
    const creation = creations.find(item => item.id === choice.creation) || creations[0];
    const version = creation.versions.find(item => item.id === choice.version) || creation.versions[0];
    choice.creation = creation.id;
    choice.version = version.id;
    const signature = JSON.stringify([this.workspace.workspace_id, creations, choice]);
    if (signature !== this.creationSignature) {
      this.creationSignature = signature;
      const options = creations.map(item => new Option(item.title, item.id));
      this.find('.visual-creation').replaceChildren(...options);
      this.find('.visual-creation').value = creation.id;
      this.find('.visual-version').replaceChildren(...creation.versions.map(item => new Option(`v${item.number} · ${this.date(item.created_at)}`, item.id)));
      this.find('.visual-version').value = version.id;
    }
    this.find('.visual-controls').hidden = false;
    const download = this.find('.visual-download');
    download.href = this.deskUrl(version.download_url, '/api/visual/download');
    const source = version.source || {};
    this.find('.visual-provenance').textContent = [source.mode === 'imported' ? 'Imported' : 'Captured', this.date(version.created_at), source.provider,
      source.thread_id && `conversation …${source.thread_id.slice(-8)}`, source.pane && `pane ${source.pane}`, this.size(version.byte_size)].filter(Boolean).join(' · ');
    const key = `${this.workspace.workspace_id}:${version.id}`;
    if (this.previewKey !== key) this.renderVersion(creation, version, key);
  }

  clearPreview() {
    this.previewEpoch += 1;
    this.previewRequest?.abort();
    this.previewRequest = null;
    this.previewKey = null;
    this.preview.replaceChildren();
    if (this.blobUrl) URL.revokeObjectURL(this.blobUrl);
    this.blobUrl = null;
  }

  async renderVersion(creation, version, key) {
    this.clearPreview();
    this.previewKey = key;
    const epoch = this.previewEpoch;
    this.empty('Loading saved version…');
    try {
      if (creation.kind === 'html') {
        const frame = this.frame(creation.title, 'allow-scripts');
        frame.src = this.deskUrl(version.preview_url, '/visual-preview/');
        this.preview.replaceChildren(frame);
        return;
      }
      this.previewRequest = new AbortController();
      const response = await fetch(this.deskUrl(version.content_url, '/api/visual/content'), {signal: this.previewRequest.signal});
      if (!response.ok) throw new Error('This saved version could not be loaded. Try downloading the original.');
      if (['markdown', 'text'].includes(creation.kind)) {
        const text = await response.text();
        if (epoch !== this.previewEpoch) return;
        if (creation.kind === 'markdown' && window.marked && window.DOMPurify) {
          try {
            const frame = this.frame(creation.title, '');
            const html = DOMPurify.sanitize(marked.parse(text), {ALLOWED_TAGS: ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br', 'hr', 'strong', 'em', 'del', 'blockquote', 'ul', 'ol', 'li', 'pre', 'code', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a'], ALLOWED_ATTR: []});
            frame.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'"><style>html{color-scheme:light;font:14px/1.65 system-ui,sans-serif;color:#23392c;background:#fff}body{margin:0;padding:24px;overflow-wrap:anywhere}h1,h2,h3{line-height:1.25}h1{font-size:25px}h2{font-size:20px}pre{padding:14px;background:#f2f5f3;border-radius:6px;white-space:pre-wrap}code{font-size:12px}blockquote{margin-left:0;padding-left:14px;border-left:3px solid #b6cec0;color:#536c5e}table{border-collapse:collapse;width:100%;font-size:12px}td,th{padding:7px;border:1px solid #dce5df;text-align:left}a{color:#287247;text-decoration:underline}</style></head><body>${html}</body></html>`;
            this.preview.replaceChildren(frame);
            return;
          } catch { /* Preserve readable source when the optional Markdown renderer fails. */ }
        }
        const original = document.createElement('pre');
        original.textContent = text;
        this.preview.replaceChildren(original);
        return;
      }
      const bytes = await response.arrayBuffer();
      if (epoch !== this.previewEpoch) return;
      if (!['image', 'pdf'].includes(creation.kind)) throw new Error('Preview unavailable. Download the original to open it.');
      const mime = creation.kind === 'pdf' ? 'application/pdf' : response.headers.get('Content-Type')?.split(';')[0];
      if (creation.kind === 'image' && !['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif'].includes(mime)) throw new Error('This image format is available as an original download.');
      this.blobUrl = URL.createObjectURL(new Blob([bytes], {type: mime}));
      if (creation.kind === 'image') {
        const image = document.createElement('img');
        image.alt = creation.title;
        image.src = this.blobUrl;
        image.onerror = () => { if (epoch === this.previewEpoch) this.empty('Image preview unavailable', 'Download the original to open this file.'); };
        this.preview.replaceChildren(image);
      } else {
        const pdf = document.createElement('object');
        pdf.type = 'application/pdf';
        pdf.data = this.blobUrl;
        pdf.setAttribute('aria-label', creation.title);
        const fallback = document.createElement('p');
        fallback.className = 'visual-message';
        fallback.textContent = 'PDF preview is unavailable in this browser. Use Download original to open the PDF.';
        pdf.append(fallback);
        const note = document.createElement('p');
        note.className = 'visual-provenance';
        note.textContent = 'PDF not displaying? Use Download original to open it in your PDF reader.';
        this.preview.replaceChildren(pdf, note);
      }
    } catch (error) {
      if (epoch !== this.previewEpoch || error.name === 'AbortError') return;
      this.empty('Preview unavailable', error.message);
    }
  }

  frame(title, sandbox) {
    const frame = document.createElement('iframe');
    frame.title = title;
    frame.setAttribute('sandbox', sandbox);
    frame.referrerPolicy = 'no-referrer';
    return frame;
  }

  deskUrl(value, prefix) {
    const url = new URL(value, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith(prefix)) throw new Error('The saved output has an invalid address.');
    return url.href;
  }

  empty(title, message = '') {
    const box = document.createElement('div');
    box.className = 'visual-message';
    const heading = document.createElement('strong');
    heading.textContent = title;
    const detail = document.createElement('span');
    detail.textContent = message;
    box.append(heading, detail);
    this.preview.replaceChildren(box);
  }

  renderLive() {
    if (!this.open || this.mode !== 'live' || document.hidden) return;
    const live = this.workspace?.live;
    if (!live || live.pane !== this.context?.pane || live.identity !== this.context?.identity) {
      this.stopLive();
      this.liveStatus('idle', live ? 'This browser belongs to another pane. Select its pane to view it.' : 'No browser connected to this pane.');
      return;
    }
    const key = `${this.workspace.workspace_id}:${live.id}:${live.generation}:${this.context.key}`;
    if (['stale', 'disconnected'].includes(live.status)) {
      this.stopLive();
      this.failedLiveKey = key;
      this.liveStatus(live.status, live.error || 'The browser connection ended. Register a current session to continue.');
      return;
    }
    if (this.liveKey === key || this.failedLiveKey === key) return;
    this.stopLive();
    this.liveKey = key;
    this.frameSequence = -1;
    this.liveStatus('connecting', `Connecting to ${live.name || live.session}…`);
    const epoch = this.epoch;
    let source;
    try { source = new EventSource(this.deskUrl(live.stream_url, '/api/visual/live')); }
    catch (error) { this.failLive(key, 'disconnected', error.message); return; }
    this.stream = source;
    const current = () => this.stream === source && this.liveKey === key && this.epoch === epoch;
    source.addEventListener('status', event => {
      if (!current()) return;
      let status;
      try { status = JSON.parse(event.data); } catch { this.failLive(key, 'disconnected', 'Invalid browser status.'); return; }
      if (status.generation !== live.generation) return;
      if (['stale', 'disconnected'].includes(status.status)) this.failLive(key, status.status, status.error || 'Browser disconnected.');
      else this.liveStatus(status.status, status.status === 'live' ? `${live.name || live.session} · live` : 'Waiting for browser frames…');
    });
    source.addEventListener('frame', event => {
      if (!current()) return;
      let frame;
      try { frame = JSON.parse(event.data); } catch { return; }
      if (frame.generation !== live.generation || !Number.isSafeInteger(frame.seq) || frame.seq <= this.frameSequence || !['image/jpeg', 'image/png'].includes(frame.mime) || typeof frame.data !== 'string' || frame.data.length > 16 * 1024 * 1024) return;
      const image = new Image();
      image.onload = () => {
        if (!current() || frame.seq <= this.frameSequence || !image.naturalWidth || !image.naturalHeight) return;
        this.frameSequence = frame.seq;
        this.canvas.width = image.naturalWidth;
        this.canvas.height = image.naturalHeight;
        this.canvas.getContext('2d').drawImage(image, 0, 0);
        this.canvas.hidden = false;
        this.find('.visual-live-stage .visual-message').hidden = true;
        this.liveStatus('live', `${live.name || live.session} · live`);
      };
      image.src = `data:${frame.mime};base64,${frame.data}`;
    });
    source.onerror = () => { if (current()) this.failLive(key, 'disconnected', 'Browser disconnected. Reconnect the view when it is available.'); };
  }

  failLive(key, status, message) {
    this.stopLive();
    this.failedLiveKey = key;
    this.liveStatus(status, message);
  }

  stopLive() {
    this.stream?.close();
    this.stream = null;
    this.liveKey = null;
    this.canvas.hidden = true;
    this.canvas.width = 0;
    this.canvas.height = 0;
    this.find('.visual-live-stage .visual-message').hidden = false;
  }

  liveStatus(status, message) {
    const label = this.find('.visual-status');
    label.dataset.status = status;
    label.textContent = message;
    this.find('.visual-live-stage .visual-message').textContent = message;
    this.find('.visual-retry').hidden = status !== 'disconnected';
  }

  date(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString([], {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
  }

  size(value) {
    return value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(value / 1024))} KB`;
  }
}
