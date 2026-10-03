const JARVIS_TARGET = Object.freeze({tab: 'jarvis', pane: 'jarvis', identity: 'jarvis'});

class JarvisClient {
  constructor({fetch, newId, retain, onChange = () => {}, now = Date.now, deadlineMs = 90000, lostGraceMs = 10000, clientId = null}) {
    this.fetch = fetch;
    this.clientId = clientId || 'client-' + Math.random().toString(36).slice(2) + now();
    this.semantic = {decide: (payload, signal) => this.decideTurn(payload, signal), invalidate: payload => this.invalidateTurn(payload)};
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
      ...(turn.result?.readback ? {readback: turn.result.readback} : {}),
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
  turnDecisionRequest(payload) {
    return {session_id: this.sessionId, client_id: this.clientId, ...payload, subject: this.snapshot?.subject ?? null};
  }
  async decideTurn(payload, signal) {
    if (!this.sessionId) throw new Error('Jarvis is unavailable');
    const sent = this.turnDecisionRequest(payload);
    const response = await this.fetch('/api/jarvis/turn-decision', {method: 'POST', headers: {'Content-Type': 'application/json'},
      signal, body: JSON.stringify(sent)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Turn decision failed');
    const subject = canonical(sent.subject);
    const current = result.session_id === this.sessionId && sent.session_id === this.sessionId
      && result.client_id === sent.client_id && result.transcript_id === sent.transcript_id
      && result.transcript_revision === sent.transcript_revision
      && canonical(result.subject ?? null) === subject && canonical(this.snapshot?.subject ?? null) === subject;
    if (!current) return {decision: 'wait', stale: true};
    const decision = result.decision;
    if (decision === 'complete' && typeof result.decision_id === 'string' && result.decision_id) {
      return {decision, decision_id: result.decision_id, correction: result.correction === true};
    }
    return {decision: decision === 'cancel' ? 'cancel' : 'wait', reason: result.reason, correction: result.correction === true};
  }
  invalidateTurn(payload) {
    if (!this.sessionId) return;
    this.fetch('/api/jarvis/turn-decision', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({...this.turnDecisionRequest(payload), invalidate: true})}).catch(() => {});
  }
  async submit(text, source = 'voice', meta = {}) {
    if (!this.sessionId) await this.refresh().catch(() => {});
    const id = this.newId();
    if (!this.sessionId) {
      this.retain({id, text, source, recipient: 'Jarvis', recoverable: true, target: this.target,
        status: 'Jarvis is unavailable. Nothing was sent. Your words are saved.'});
      this.onChange('Jarvis is unavailable. Nothing was sent. Your words are saved.');
      return null;
    }
    const entry = {id, text, source, meta, sessionId: this.sessionId, generation: this.voice.stopReadback('new request'),
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
          source: entry.source, stream: !!this.voice.capture?.ready, client_id: this.clientId,
          ...(entry.meta.transcript_id ? {transcript_id: entry.meta.transcript_id, transcript_revision: entry.meta.transcript_revision} : {}),
          ...(entry.meta.decision_id ? {decision_id: entry.meta.decision_id} : {explicit_send: entry.meta.explicit_send !== false})})
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
    const readback = readbackOf(receipt);
    const track = (player, {style}) => this.trackPlayback(player, {generation: entry.generation, turnId: receipt.turn_id, style});
    if (receipt.status === 'completed' && readback) {
      entry.player?.stop();
      if (current) this.voice.speakReply(readback.original, entry.generation, false, this.heardCallback(readback.reply_ref), track);
    } else if (receipt.status === 'completed' && receipt.text) {
      if (!current) return entry.player?.stop();
      if (entry.player) {
        const exact = receipt.text.startsWith(entry.streamed);
        const rest = entry.streamed.includes(receipt.text) ? ''
          : exact ? receipt.text.slice(entry.streamed.length) : ' ' + receipt.text;
        this.voice.presentReply(receipt.text);
        if (rest) entry.player.write(rest);
        entry.player.end();
        if (exact) track(entry.player, {style: 'original'});
      } else this.voice.speakReply(receipt.text, entry.generation, true, null, track);
    } else {
      entry.player?.stop();
      if (receipt.status === 'failed' && current) {
        const canonical = receipt.actions.length && typeof receipt.text === 'string' ? receipt.text.trim() : '';
        this.voice.speak(canonical || 'Jarvis could not complete that request. The details are on screen.', entry.generation);
      }
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
    const readback = readbackOf(receipt);
    const status = [{running: 'Jarvis is working…', completed: 'Answered', cancelled: 'Superseded or cancelled. Receipts below remain.',
      failed: 'Jarvis could not complete this: ' + (receipt.error || 'unknown error')}[receipt.status] || receipt.status,
      ...(readback && receipt.text ? [receipt.text] : []), ...receipt.actions.map(actionLine), ...jobs.map(job => `Job · ${job.subject?.label || 'conversation'}: ${job.status}`)].join('\n');
    const signature = JSON.stringify([status, receipt.text, delivered?.payload, readback]);
    if (this.rendered.get(receipt.request_id) === signature) return;
    this.rendered.set(receipt.request_id, signature);
    this.retain({id: receipt.request_id, text, source, recipient: 'Jarvis', status,
      deliveredText: delivered?.payload,
      ...(readback ? {reply: {id: receipt.request_id, target: readback.target?.label || readback.target?.tab || 'Agent',
        text: readback.original, heard_reply: readback.reply_ref}}
        : receipt.status === 'completed' && receipt.text ? {reply: {id: receipt.request_id, target: 'Jarvis', text: receipt.text}} : {})});
  }
  trackPlayback(player, {generation, turnId, style}) {
    const sessionId = this.sessionId;
    if (!sessionId || !turnId || typeof player?.track !== 'function') return;
    const ledger = {id: null, boundary: null, dead: false, sent: null, queue: Promise.resolve()};
    const post = async body => {
      const response = await this.fetch('/api/jarvis/playback', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({session_id: sessionId, ...body})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Playback rejected');
      return result;
    };
    const report = async progress => {
      if (ledger.dead || sessionId !== this.sessionId) return;
      const terminal = progress.status !== 'playing' && progress.status !== 'generated';
      if (progress.status === 'generated' || ledger.sent === progress.status) return;
      try {
        if (!ledger.id) {
          const created = await post({op: 'create', turn_id: turnId, client_id: this.clientId, epoch: generation, style});
          if (created.turn_id !== turnId || created.client_id !== this.clientId || created.epoch !== generation
            || typeof created.playback_id !== 'string') throw new Error('Playback record does not match');
          ledger.id = created.playback_id;
          ledger.boundary = created.full_boundary_id;
        }
        const complete = progress.status === 'completed' && typeof ledger.boundary === 'string' && ledger.boundary;
        await post({op: 'update', playback_id: ledger.id, client_id: this.clientId, epoch: generation,
          status: progress.status === 'completed' && !complete ? 'failed' : progress.status,
          completed_phrases: complete ? [ledger.boundary] : []});
        ledger.sent = progress.status;
        if (terminal) ledger.dead = true;
      } catch { ledger.dead = true; }
    };
    player.track(progress => { ledger.queue = ledger.queue.then(() => report(progress)); });
  }
  heardCallback(reference) {
    if (!reference) return null;
    return () => this.fetch('/api/replies/heard', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(reference)}).then(response => { if (!response.ok) throw new Error('rejected'); })
      .catch(() => this.onChange('Could not record that the reply was heard.'));
  }
  async supersedeForDirect() {
    const owned = [...this.pending.values()].filter(entry => !entry.settled);
    this.latestRequestId = null;
    await Promise.all(owned.map(entry => this.fetch('/api/jarvis/cancel', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: entry.sessionId, request_id: entry.id})}).catch(() => {})));
  }
}

function readbackOf(receipt) {
  const {original, target, reply_ref: reference} = receipt.readback || {};
  if (typeof original !== 'string' || !original) return null;
  const plain = value => value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  return {original, target: plain(target), reply_ref: plain(reference)};
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

function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  return JSON.stringify(value ?? null);
}
