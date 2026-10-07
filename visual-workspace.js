class VisualWorkspace {
  constructor(root, toggle, options = {}) {
    this.root = root;
    this.toggle = toggle;
    this.conversationToggle = options.conversationToggle;
    this.options = options;
    try { this.preferences = JSON.parse(localStorage.getItem('jev-visual-preferences') || '{}'); } catch { this.preferences = {}; }
    this.context = null;
    this.epoch = 0;
    this.previewEpoch = 0;
    this.choices = new Map();
    this.staleLiveKeys = new Set();
    this.mode = 'creations';
    this.open = false;
    this.lastRefresh = 0;
    this.root.innerHTML = `
      <div class="visual-context"><select class="visual-pane" aria-label="Visual workspace pane"></select></div>
      <div class="visual-tabs" role="tablist" aria-label="Visual workspace views">
        <button type="button" id="visual-creations-tab" role="tab" aria-controls="visual-creations" aria-selected="true">Creations<span class="visual-count"></span></button>
        <button type="button" id="visual-live-tab" role="tab" aria-controls="visual-live" aria-selected="false" tabindex="-1">Live browser</button>
      </div>
      <section id="visual-creations" class="visual-panel" role="tabpanel" aria-labelledby="visual-creations-tab">
        <details class="creation-picker" hidden><summary>Saved outputs <span class="creation-selected"></span></summary><div class="creation-list" role="group" aria-label="Saved outputs"></div></details><div class="reader-mode"><button type="button" data-reader="read" aria-pressed="true">Read</button><button type="button" data-reader="source" aria-pressed="false">Source</button><label class="image-zoom-label" hidden>Local zoom<select class="image-zoom"><option value="fit">Fit</option><option value="1">100%</option><option value="2">200%</option><option value="3">300%</option></select></label></div><div class="visual-preview"></div>
        <div class="visual-provenance"></div>
      </section>
      <section id="visual-live" class="visual-panel" role="tabpanel" aria-labelledby="visual-live-tab" hidden><div class="managed-browser" hidden></div><div class="legacy-browser visual-panel">
        <p class="visual-status" role="status"></p>
        <div class="visual-live-stage"><canvas aria-label="Live browser view" hidden></canvas><p class="visual-message"></p></div>
        <div class="visual-live-footer"><span>View only · follows the selected pane</span><button type="button" class="visual-retry" hidden>Reconnect view</button></div></div>
      </section><section class="visual-apps" hidden></section>`;
    this.find = selector => this.root.querySelector(selector);
    this.preview = this.find('.visual-preview');
    this.canvas = this.find('.legacy-browser canvas');
    if (typeof ManagedBrowserView !== 'undefined') this.browser = new ManagedBrowserView(this.find('.managed-browser'), {clientId: options.clientId || crypto.randomUUID(), activity: (id, mode) => this.activity(id, mode), preference: () => this.preference(), save: () => this.savePreferences()});
    if (this.conversationToggle) this.conversationToggle.onclick = () => this.close();
    this.find('.visual-pane').onchange = event => this.options.selectPane?.(event.target.dataset.tab, event.target.value);
    for (const button of this.root.querySelectorAll('[data-reader]')) button.onclick = () => { this.preference().readerMode = button.dataset.reader; this.savePreferences(); this.clearPreview(); this.renderCreations(); };
    this.find('.image-zoom').onchange = event => { this.readingPreference().imageZoom = event.target.value; this.savePreferences(); this.zoomImage(); };
    this.preview.onscroll = () => { if (this.previewKey) { this.readingPreference().scroll = this.preview.scrollTop; this.savePreferences(); } };
    addEventListener('message', event => {
      if (!this.readerFrame || event.source !== this.readerFrame.contentWindow || event.data?.type !== 'jev-reader-scroll' || event.data.token !== this.readerToken || !Number.isFinite(event.data.y)) return;
      this.readingPreference().scroll = Math.max(0, event.data.y); this.savePreferences();
    });
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
    this.toggle.onclick = () => {
      if (this.toggle.textContent.includes('new')) {
        if (this.pendingMode === 'creations') {
          const latest = this.workspace?.creations.flatMap(creation => creation.versions.map(version => ({creation, version})))
            .sort((a, b) => new Date(b.version.created_at) - new Date(a.version.created_at))[0];
          if (latest) Object.assign(this.choice(), {creation: latest.creation.id, version: latest.version.id});
        }
        this.preference().historical = false;
        this.toggle.textContent = 'Visuals';
        this.setMode(this.pendingMode || 'creations');
        this.pendingMode = null;
      }
      this.open = true; this.preference().closed = false; this.savePreferences(); this.renderOpen();
    };
    this.find('.visual-retry').onclick = () => {
      this.retryLiveKey = this.failedLiveKey;
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
    const key = JSON.stringify([tab?.workspace_id, tab?.id, pane?.id, pane?.identity]);
    if (key !== this.context?.key) {
      this.suspend();
      this.context = tab && pane ? {key, tab: tab.id, pane: pane.id, identity: pane.identity, name: tab.name, workspace_id: tab.workspace_id} : null;
      this.workspace = null;
      this.creationSignature = null;
      this.pendingMode = null;
      this.toggle.textContent = 'Visuals';
      this.find('.creation-picker').hidden = true;
      this.find('.visual-count').textContent = '';
      this.find('.visual-provenance').textContent = '';
      this.empty('Loading this tab’s creations…');
    }
    if (!this.context) return;
    const paneSelect = this.find('.visual-pane');
    paneSelect.dataset.tab = tab.id;
    const signature = JSON.stringify(tab.panes.map(item => [item.id, item.identity]));
    if (paneSelect.dataset.signature !== signature) { paneSelect.replaceChildren(...tab.panes.map(item => new Option(`Pane ${item.number || item.id} · ${item.provider || item.command || 'conversation'}`, item.id))); paneSelect.dataset.signature = signature; }
    paneSelect.value = pane.id; paneSelect.hidden = tab.panes.length < 2;
    this.browser?.update(this.context);
    this.refreshApps();
    this.refresh();
  }

  suspend() {
    this.epoch += 1;
    this.browser?.update(null);
    this.context = null;
    this.workspace = null;
    this.request?.abort();
    this.request = null;
    this.lastRefresh = 0;
    this.stopLive();
    this.failedLiveKey = null;
    this.retryLiveKey = null;
    this.clearPreview();
    this.find('.creation-picker').hidden = true;
    this.find('.visual-provenance').textContent = '';
    this.find('.visual-count').textContent = '';
    this.find('.creation-list').replaceChildren();
    this.find('.reader-mode').hidden = true;
    this.find('.visual-apps').hidden = true;
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
      const previousWorkspace = this.workspace?.workspace_id;
      this.workspace = data;
      if (previousWorkspace !== data.workspace_id) {
        this.choices.set(data.workspace_id, this.preference(data.workspace_id));
        const preference = this.preference();
        this.mode = preference.mode || 'creations';
        this.open = preference.closed === true ? false : preference.open ?? false;
        this.setMode(this.mode, false); this.renderOpen();
      }
      const latest = data.creations.flatMap(item => item.versions).sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
      if (latest && !latest.source?.app) this.activity(latest.id, 'creations');
      this.find('.visual-count').textContent = String(data.creations.reduce((total, item) => total + item.versions.length, 0));
      this.renderCreations();
      this.renderLive();
    } catch (error) {
      if (epoch !== this.epoch || error.name === 'AbortError') return;
      this.workspace = null;
      this.clearPreview();
      this.stopLive();
      this.find('.creation-picker').hidden = true;
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
    this.toggle.setAttribute('aria-pressed', String(this.open));
    this.conversationToggle?.setAttribute('aria-pressed', String(!this.open));
    this.preference().open = this.open; this.savePreferences();
    if (this.open) { this.refresh(); this.renderLive(); }
    else { this.stopLive(); this.clearPreview(); this.browser?.show(false); }
    if (this.open && this.workspace) this.renderCreations();
  }

  setMode(mode, remember = true) {
    this.mode = mode;
    if (remember) { this.preference().mode = mode; this.savePreferences(); }
    for (const name of ['creations', 'live']) {
      this.find(`#visual-${name}`).hidden = name !== mode;
      const button = this.find(`#visual-${name}-tab`);
      button.setAttribute('aria-selected', String(name === mode));
      button.tabIndex = name === mode ? 0 : -1;
    }
    if (mode === 'live') { this.clearPreview(); this.renderLive(); }
    else { this.stopLive(); this.browser?.show(false); this.renderCreations(); }
  }

  choice() {
    const id = this.workspace.workspace_id;
    if (!this.choices.has(id)) this.choices.set(id, this.preference(id));
    return this.choices.get(id);
  }

  renderCreations() {
    if (!this.workspace || !this.open || this.mode !== 'creations') return;
    const outputs = this.workspace.creations.flatMap(creation => creation.versions.map(version => ({creation, version})))
      .sort((a, b) => new Date(b.version.created_at) - new Date(a.version.created_at) || b.version.number - a.version.number);
    if (!outputs.length) {
      this.clearPreview();
      this.find('.creation-picker').hidden = true;
      this.find('.visual-provenance').textContent = '';
      this.find('.creation-list').replaceChildren(); this.find('.reader-mode').hidden = true;
      this.empty('No creations saved yet', 'Save an output from this tab to keep a copy and revisit each version.');
      return;
    }
    const choice = this.choice();
    const selected = outputs.find(item => item.creation.id === choice.creation && item.version.id === choice.version) || outputs[0];
    const {creation, version} = selected;
    choice.creation = creation.id;
    choice.version = version.id;
    this.savePreferences();
    const signature = JSON.stringify([this.workspace.workspace_id, this.workspace.creations]);
    if (signature !== this.creationSignature) {
      this.creationSignature = signature;
      this.find('.creation-list').replaceChildren(...outputs.map(item => {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'creation-item';
        button.dataset.version = item.version.id;
        const title = document.createElement('strong'); title.textContent = item.creation.title;
        const info = document.createElement('span'); info.textContent = `${this.date(item.version.created_at)}${item.creation.versions.length > 1 ? ` · v${item.version.number}` : ''}`;
        button.append(title, info); button.onclick = () => {
          Object.assign(choice, {creation: item.creation.id, version: item.version.id});
          this.preference().historical = item.version.id !== outputs[0].version.id;
          this.find('.creation-picker').open = false;
          this.savePreferences(); this.renderCreations();
        };
        return button;
      }));
    }
    this.find('.creation-picker').hidden = false;
    this.find('.creation-selected').textContent = `${creation.title} · ${this.date(version.created_at)}`;
    for (const button of this.root.querySelectorAll('.creation-item')) button.setAttribute('aria-pressed', String(button.dataset.version === version.id));
    const source = version.source || {};
    this.find('.visual-provenance').textContent = [source.mode === 'imported' ? 'Imported' : 'Captured', this.date(version.created_at), source.provider,
      source.thread_id && `conversation …${source.thread_id.slice(-8)}`, source.pane && `pane ${source.pane}`, this.size(version.byte_size)].filter(Boolean).join(' · ');
    const readerMode = this.preference().readerMode || 'read';
    this.find('.reader-mode').hidden = !['html', 'markdown', 'text', 'image'].includes(creation.kind);
    this.find('[data-reader="source"]').hidden = !['html', 'markdown'].includes(creation.kind);
    this.find('.image-zoom-label').hidden = creation.kind !== 'image';
    for (const button of this.root.querySelectorAll('[data-reader]')) button.setAttribute('aria-pressed', String(button.dataset.reader === readerMode));
    const key = `${this.workspace.workspace_id}:${version.id}:${readerMode}`;
    if (this.previewKey !== key) this.renderVersion(creation, version, key);
  }

  clearPreview() {
    this.pdf?.destroy(); this.pdf = null; this.readerFrame = null; this.readerToken = null;
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
      if (creation.kind === 'html' && this.preference().readerMode !== 'source') {
        const frame = this.frame(creation.title, 'allow-scripts');
        frame.src = this.deskUrl(version.preview_url, '/visual-preview/');
        this.preview.replaceChildren(frame);
        return;
      }
      this.previewRequest = new AbortController();
      const response = await fetch(this.deskUrl(version.content_url, '/api/visual/content'), {signal: this.previewRequest.signal});
      if (!response.ok) throw new Error('This saved output could not be loaded.');
      if (['markdown', 'text', 'html'].includes(creation.kind)) {
        const text = await response.text();
        if (epoch !== this.previewEpoch) return;
        if (creation.kind === 'markdown' && this.preference().readerMode !== 'source' && window.marked && window.DOMPurify) {
          try {
            const frame = this.frame(creation.title, 'allow-scripts allow-popups allow-popups-to-escape-sandbox');
            const markdown = markdownDocument(text, version, this.readingPreference().scroll || 0);
            this.readerFrame = frame; this.readerToken = markdown.token;
            frame.srcdoc = markdown.html;
            this.preview.replaceChildren(frame);
            return;
          } catch { /* Preserve readable source when the optional Markdown renderer fails. */ }
        }
        const original = document.createElement('pre');
        original.textContent = text;
        this.preview.replaceChildren(original);
        this.preview.scrollTop = this.readingPreference().scroll || 0;
        return;
      }
      const bytes = await response.arrayBuffer();
      if (epoch !== this.previewEpoch) return;
      if (!['image', 'pdf'].includes(creation.kind)) throw new Error('Preview unavailable for this file type.');
      const mime = creation.kind === 'pdf' ? 'application/pdf' : response.headers.get('Content-Type')?.split(';')[0];
      if (creation.kind === 'image' && !['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif'].includes(mime)) throw new Error('Preview unavailable for this image format.');
      this.blobUrl = URL.createObjectURL(new Blob([bytes], {type: mime}));
      if (creation.kind === 'image') {
        const image = document.createElement('img');
        image.alt = creation.title;
        image.src = this.blobUrl;
        image.onerror = () => { if (epoch === this.previewEpoch) this.empty('Image preview unavailable'); };
        this.preview.replaceChildren(image); this.zoomImage();
        this.preview.scrollTop = this.readingPreference().scroll || 0;
      } else {
        const host = document.createElement('div'); host.className = 'pdf-reader'; this.preview.replaceChildren(host);
        this.pdf = new PdfReader(host, this.readingPreference(), () => this.savePreferences());
        await this.pdf.load(bytes);

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
    const managed = this.browser && (!this.workspace?.live || this.browser.pages.length > 0);
    this.find('.legacy-browser').hidden = !!managed;
    this.browser?.show(!!managed);
    if (managed) { this.stopLive(); return; }
    const live = this.workspace?.live;
    if (!live || live.pane !== this.context?.pane || live.identity !== this.context?.identity) {
      this.stopLive();
      this.liveStatus('idle', live ? 'This browser belongs to another pane. Select its pane to view it.' : 'No browser connected to this pane.');
      return;
    }
    const key = `${this.workspace.workspace_id}:${live.id}:${live.generation}:${this.context.key}`;
    if (live.status === 'stale' || this.staleLiveKeys.has(key)) {
      this.stopLive();
      this.failedLiveKey = key;
      this.staleLiveKeys.add(key);
      this.retryLiveKey = null;
      this.liveStatus('stale', live.error || 'The browser identity changed. Register a current session to continue.');
      return;
    }
    if (this.liveKey === key) return;
    const retry = this.retryLiveKey === key;
    if (live.status === 'disconnected' && !retry) {
      this.stopLive();
      this.failedLiveKey = key;
      this.liveStatus('disconnected', live.error || 'Browser disconnected. Reconnect the view when it is available.');
      return;
    }
    if (this.failedLiveKey === key && !retry) return;
    this.stopLive();
    this.retryLiveKey = null;
    this.failedLiveKey = null;
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
    if (status === 'stale') this.staleLiveKeys.add(key);
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

  preference(id = this.workspace?.workspace_id || this.context?.workspace_id || this.context?.tab || 'initial') {
    return this.preferences[id] ||= {};
  }

  readingPreference() {
    const preference = this.preference();
    preference.readers ||= {};
    return preference.readers[this.workspace ? this.choice().version || 'initial' : 'initial'] ||= {};
  }

  savePreferences() {
    try { localStorage.setItem('jev-visual-preferences', JSON.stringify(this.preferences)); } catch {}
  }

  close() {
    this.open = false; this.preference().closed = true; this.savePreferences(); this.renderOpen();
  }

  activity(id, mode) {
    if (!id || !this.context) return;
    const preference = this.preference();
    const key = 'seen_' + mode;
    if (preference[key] === id) return;
    preference[key] = id;
    if (!this.open || preference.historical || this.mode !== mode) {
      this.pendingMode = mode;
      this.toggle.textContent = 'Visuals · new'; this.savePreferences(); return;
    }
    this.toggle.textContent = 'Visuals';
    this.open = true;
    if (mode === 'creations' && this.workspace?.creations.length) {
      const creation = this.workspace.creations.find(item => item.versions.some(version => version.id === id)) || this.workspace.creations[0];
      Object.assign(this.choice(), {creation: creation.id, version: id});
    }
    this.setMode(mode); this.renderOpen(); this.savePreferences();
  }

  reference() {
    if (!this.open || this.mode !== 'creations' || !this.workspace) return null;
    const choice = this.choice(), creation = this.workspace.creations.find(item => item.id === choice.creation);
    const version = creation?.versions.find(item => item.id === choice.version);
    return version ? {workspace_id: this.workspace.workspace_id, creation_id: creation.id, version_id: version.id, source: Object.fromEntries(['provider', 'thread_id', 'pane', 'identity'].filter(key => version.source?.[key] !== undefined).map(key => [key, version.source[key]]))} : null;
  }

  zoomImage() {
    const image = this.preview.querySelector('img'); if (!image) return;
    const value = this.readingPreference().imageZoom || 'fit'; this.find('.image-zoom').value = value;
    image.style.width = value === 'fit' ? '100%' : `${Number(value) * 100}%`;
    image.style.maxWidth = 'none'; image.style.height = value === 'fit' ? '100%' : 'auto';
  }

  async refreshApps() {
    if (!this.context || this.appLoading || Date.now() - (this.lastApps || 0) < 2000) return;
    const epoch = this.epoch; this.appLoading = true; this.lastApps = Date.now();
    try {
      const response = await fetch('/api/apps/state?' + new URLSearchParams({tab: this.context.tab}));
      const result = await response.json(); if (epoch !== this.epoch || !response.ok) return;
      const root = this.find('.visual-apps'); root.hidden = !result.apps.length;
      root.replaceChildren(...result.apps.map(app => {
        const card = document.createElement('div'); card.className = 'app-card';
        const title = document.createElement('strong'); title.textContent = `${app.title} · ${app.status}`;
        const detail = document.createElement('pre'); detail.textContent = app.error || app.log_excerpt || (app.status === 'ready' ? 'Running app · current state, separate from saved versions' : '');
        card.append(title, detail);
        if (app.status === 'ready') { const open = document.createElement('button'); open.type = 'button'; open.textContent = 'View app'; open.onclick = async () => { if (this.context.pane !== app.target.pane) await this.options.selectPane?.(app.target.tab, app.target.pane); this.preference().browserPage = app.page_id; this.open = true; this.setMode('live'); this.renderOpen(); this.browser?.refresh(true); }; card.append(open); }
        if (['starting', 'ready'].includes(app.status)) { const stop = document.createElement('button'); stop.type = 'button'; stop.textContent = 'Stop app'; stop.onclick = async () => { stop.disabled = true; try { const r = await fetch('/api/apps/stop', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({target: app.target, app_id: app.id})}); const value = await r.json(); if (!r.ok) throw new Error(value.error); this.lastApps = 0; this.refreshApps(); } catch (error) { detail.textContent = error.message; stop.disabled = false; } }; card.append(stop); }
        return card;
      }));
    } catch {} finally { this.appLoading = false; }
  }

  date(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString([], {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
  }

  size(value) {
    return value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(value / 1024))} KB`;
  }
}
