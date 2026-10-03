const JARVIS_TARGET = Object.freeze({tab: 'jarvis', pane: 'jarvis', identity: 'jarvis'});

class JarvisClient {
  constructor({fetch, newId, retain, onChange = () => {}, now = Date.now, deadlineMs = 90000, lostGraceMs = 10000}) {
    this.fetch = fetch;
    this.newId = newId;
    this.retain = retain;
    this.onChange = onChange;
    this.now = now;
    this.deadlineMs = deadlineMs;
    this.lostGraceMs = lostGraceMs;
    this.voice = null;
    this.sessionId = null;
    this.revision = -1;
    this.snapshot = null;
    this.pending = new Map();
    this.latestRequestId = null;
    this.rendered = new Map();
  }
  get target() { return JARVIS_TARGET; }
  get summary() {
    const snapshot = this.snapshot;
    const jobs = Object.values(snapshot?.jobs || {}).filter(job => ['queued', 'running'].includes(job.status));
    return {
      available: !!snapshot,
      subject: snapshot?.subject ? snapshot.subject.label || snapshot.subject.tab : null,
      jobs: jobs.map(job => ({label: job.subject?.label || job.subject?.tab || 'Job', status: job.status})),
      busy: this.pending.size > 0,
    };
  }
  async refresh() {
    const startedAt = this.now();
    const response = await this.fetch('/api/jarvis/state');
    const snapshot = await response.json();
    if (!response.ok) throw new Error(snapshot.error || 'Jarvis state unavailable');
    return this.adopt(snapshot, startedAt);
  }
  adopt(snapshot, startedAt = this.now()) {
    if (!snapshot || typeof snapshot.session_id !== 'string' || !Number.isInteger(snapshot.revision)) return false;
    if (this.sessionId !== snapshot.session_id) {
      const previous = this.sessionId;
      this.sessionId = snapshot.session_id;
      this.revision = -1;
      this.rendered.clear();
      if (previous) for (const entry of [...this.pending.values()]) {
        this.settle(entry, null, 'Jarvis restarted, so this request cannot be matched to a receipt.');
      }
    } else if (snapshot.revision < this.revision) return false;
    this.revision = snapshot.revision;
    this.snapshot = snapshot;
    for (const turn of Object.values(snapshot.turns || {})) {
      if (turn.mode === 'jarvis') this.show(this.receiptFromState(turn), turn.user_text, turn.source);
    }
    for (const entry of [...this.pending.values()]) this.reconcile(entry, startedAt);
    this.onChange();
    return true;
  }
  receiptFromState(turn) {
    const snapshot = this.snapshot;
    return {session_id: snapshot.session_id, turn_id: turn.turn_id, request_id: turn.request_id,
      revision: snapshot.revision, status: turn.status, text: turn.result?.text || '',
      actions: Object.values(snapshot.actions || {}).filter(action => action.turn_id === turn.turn_id),
      ...(turn.error ? {error: turn.error} : {})};
  }
  reconcile(entry, startedAt) {
    const turn = this.snapshot.turns?.[this.snapshot.requests?.[entry.id]];
    if (turn) {
      if (turn.status !== 'running') this.settle(entry, this.receiptFromState(turn));
    } else if (entry.lost && startedAt >= entry.lostAt && this.now() - entry.lostAt >= this.lostGraceMs) {
      this.settle(entry, null, 'Jarvis has no record of this request. It was not resent.');
    }
  }
  isCurrent(entry) {
    return this.latestRequestId === entry.id && this.voice?.generation === entry.generation;
  }
  async submit(text, source = 'voice') {
    if (!this.sessionId) await this.refresh().catch(() => {});
    const id = this.newId();
    if (!this.sessionId) {
      this.retain({id, text, source, recipient: 'Jarvis', recoverable: true, target: this.target,
        status: 'Jarvis is unavailable. Nothing was sent. Your words are saved.'});
      this.onChange('Jarvis is unavailable. Nothing was sent. Your words are saved.');
      return null;
    }
    const entry = {id, text, source, sessionId: this.sessionId, generation: this.voice.stopReadback('new request'),
      streamed: '', player: null, settled: false, lost: false};
    entry.done = new Promise(resolve => { entry.resolve = resolve; });
    this.pending.set(id, entry);
    this.latestRequestId = id;
    this.retain({id, text, source, recipient: 'Jarvis', target: this.target, status: 'Jarvis is working…'});
    entry.timer = setTimeout(() => this.settle(entry, null, 'No outcome arrived in time.'), this.deadlineMs);
    this.onChange();
    this.post(entry);
    return entry.done;
  }
  async post(entry) {
    entry.controller = new AbortController();
    try {
      const response = await this.fetch('/api/jarvis/turn', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, signal: entry.controller.signal,
        body: JSON.stringify({session_id: entry.sessionId, request_id: entry.id, text: entry.text,
          source: entry.source, stream: !!this.voice.capture?.ready})
      });
      const receipt = await readJSONStream(response, delta => this.delta(entry, delta));
      if (!response.ok && !receipt.turn_id) {
        if (response.status >= 400 && response.status < 500) {
          return this.settle(entry, null, 'Jarvis rejected this request: ' + (receipt.error || response.status), true);
        }
        throw new Error(receipt.error || 'Jarvis request failed');
      }
      this.settle(entry, receipt);
    } catch (error) {
      if (entry.settled) return;
      entry.lost = true;
      entry.lostAt = this.now();
      this.refresh().catch(() => {});
    }
  }
  delta(entry, delta) {
    entry.streamed += delta;
    if (!this.isCurrent(entry)) return;
    this.voice.presentReply(entry.streamed);
    entry.player ||= this.voice.beginReadback(entry.generation);
    entry.player?.write(delta);
  }
  settle(entry, receipt, reason = '', rejected = false) {
    if (entry.settled) return;
    entry.settled = true;
    clearTimeout(entry.timer);
    entry.controller?.abort();
    this.pending.delete(entry.id);
    if (receipt) this.finish(entry, receipt);
    else this.unconfirmed(entry, reason, rejected);
    entry.resolve(receipt);
    this.onChange();
  }
  finish(entry, receipt) {
    this.show(receipt, entry.text, entry.source);
    const current = this.isCurrent(entry);
    if (receipt.status === 'completed' && receipt.text) {
      if (!current) return entry.player?.stop();
      if (entry.player) {
        const rest = entry.streamed.includes(receipt.text) ? ''
          : receipt.text.startsWith(entry.streamed) ? receipt.text.slice(entry.streamed.length) : ' ' + receipt.text;
        this.voice.presentReply(receipt.text);
        if (rest) entry.player.write(rest);
        entry.player.end();
      } else this.voice.speakReply(receipt.text, entry.generation, true);
    } else {
      entry.player?.stop();
      if (receipt.status === 'failed' && current) this.voice.speak('Jarvis could not complete that request. The details are on screen.', entry.generation);
    }
  }
  unconfirmed(entry, reason, rejected) {
    entry.player?.stop();
    const status = rejected ? reason + ' Your words are saved.' : 'Outcome unknown. ' + reason + ' Check Jarvis activity before repeating it.';
    this.retain({id: entry.id, status, recoverable: rejected, target: this.target});
    this.onChange(status);
    if (this.isCurrent(entry)) {
      this.voice.speak(rejected ? 'Jarvis rejected that request. Your words are saved.'
        : 'I have not confirmed the outcome. Nothing was resent. Check Jarvis activity before repeating that.', entry.generation);
    }
  }
  show(receipt, text, source) {
    const jobs = Object.values(this.snapshot?.jobs || {}).filter(job => job.turn_id === receipt.turn_id);
    const delivered = receipt.actions.find(action => action.draft_id && ['attempting', 'submitted', 'uncertain'].includes(action.status));
    const status = [{running: 'Jarvis is working…', completed: 'Answered', cancelled: 'Superseded or cancelled. Receipts below remain.',
      failed: 'Jarvis could not complete this: ' + (receipt.error || 'unknown error')}[receipt.status] || receipt.status,
      ...receipt.actions.map(actionLine), ...jobs.map(job => `Job · ${job.subject?.label || 'conversation'}: ${job.status}`)].join('\n');
    const signature = JSON.stringify([status, receipt.text, delivered?.payload]);
    if (this.rendered.get(receipt.request_id) === signature) return;
    this.rendered.set(receipt.request_id, signature);
    this.retain({id: receipt.request_id, text, source, recipient: 'Jarvis', status,
      deliveredText: delivered?.payload,
      ...(receipt.status === 'completed' && receipt.text ? {reply: {id: receipt.request_id, target: 'Jarvis', text: receipt.text}} : {})});
  }
  async supersedeForDirect() {
    if (!this.pending.size || !this.sessionId) return;
    this.latestRequestId = null;
    await this.fetch('/api/jarvis/cancel', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: this.sessionId})}).catch(() => {});
  }
}

function actionLine(action) {
  const who = action.target?.label || action.target?.tab || 'the recipient';
  if (!action.draft_id) return `${action.tool.replaceAll('_', ' ')}: ${action.status}`;
  return {
    attempting: `Sending to ${who}…`,
    submitted: `Submitted to ${who}. Submitted is not completed.`,
    uncertain: `Delivery to ${who} is uncertain. Check it before sending again; the text may already be there.`,
    failed: `Delivery to ${who} failed${action.result?.error ? ': ' + action.result.error : ''}.`,
    not_attempted: `Nothing was sent to ${who}.`,
    blocked: `Nothing was sent to ${who}.`,
    cancelled: `Delivery to ${who} was cancelled.`,
  }[action.status] || `Message to ${who}: ${action.status}`;
}
