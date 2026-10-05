class ReadbackPlayer {
  constructor(url, context, {onStart, onEnd, onError, onProgress = null, onDiagnostic = null}) {
    this.context = context;
    this.diagnostic = fields => { try { onDiagnostic?.(fields); } catch {} };
    this.sources = new Set();
    this.queue = [];
    this.text = '';
    this.nextTime = 0;
    this.closed = false;
    this.final = false;
    this.started = false;
    this.sentAll = false;
    this.status = 'generated';
    this.phrases = [];
    this.scheduledMs = 0;
    this.endedMs = 0;
    this.sourceText = '';
    this.ends = [];
    this.alignedText = '';
    this.alignmentValid = true;
    this.reportedChars = 0;
    this.onProgress = onProgress;
    this.socket = new WebSocket(url);
    this.socket.onopen = () => {
      for (const message of this.queue) this.socket.send(message);
      this.queue = [];
    };
    this.socket.onmessage = ({data}) => {
      if (this.closed) return;
      try {
        const message = JSON.parse(data);
        if (message.error) throw new Error(message.error);
        if (message.alignment) { this.diagnostic({stage:'alignment', alignment:message.alignment}); this.align(message.alignment); }
        if (message.audio) {
          const bytes = Uint8Array.from(atob(message.audio), char => char.charCodeAt(0));
          const samples = new DataView(bytes.buffer);
          const buffer = context.createBuffer(1, bytes.length / 2, 24000);
          const channel = buffer.getChannelData(0);
          for (let i = 0; i < channel.length; i++) channel[i] = samples.getInt16(i * 2, true) / 32768;
          const source = context.createBufferSource();
          source.buffer = buffer;
          source.connect(context.destination);
          source.onended = () => {
            this.sources.delete(source);
            this.endedMs += buffer.duration * 1000;
            this.diagnostic({stage:'audio_ended', ended_ms:this.endedMs, scheduled_ms:this.scheduledMs});
            source.disconnect();
            this.finishPlayback();
            this.reportHeard();
          };
          this.diagnostic({stage:'audio_queued', bytes:bytes.length, duration_ms:buffer.duration*1000, scheduled_ms:this.scheduledMs, ended_ms:this.endedMs});
          this.sources.add(source);
          this.nextTime = Math.max(context.currentTime + .03, this.nextTime);
          source.start(this.nextTime);
          this.nextTime += buffer.duration;
          this.scheduledMs += buffer.duration * 1000;
          if (!this.started) { this.started = true; this.status = 'playing'; this.report(); onStart(); }
        }
        if (message.isFinal) {
          if (!this.sentAll) throw new Error('Speech ended before the reply was complete');
          this.final = true; clearTimeout(this.timeout); this.finishPlayback();
        }
      } catch (error) { this.fail(error); }
    };
    this.socket.onerror = () => this.fail(new Error('Speech connection failed'));
    this.socket.onclose = () => {
      if (!this.closed && !this.final) this.fail(new Error('Speech ended before completion'));
    };
    this.onEnd = onEnd;
    this.onError = onError;
    this.timeout = setTimeout(() => this.fail(new Error('Speech timed out')), 90000);
  }
  send(message) {
    if (this.closed) return;
    if (message.text) this.phrases.push(message.text);
    const text = JSON.stringify(message);
    if (this.socket.readyState === WebSocket.OPEN) this.socket.send(text);
    else this.queue.push(text);
  }
  write(delta) {
    if (this.closed) return;
    this.sourceText += delta;
    this.text += delta;
    while (this.text) {
      const boundaries = [this.text.match(/^[\s\S]*?[,;:.!?]["')\]]*\s+/),
        this.text.match(/^(?:\s*\S+\s+){6}/)].filter(Boolean);
      if (!boundaries.length) break;
      const phrase = boundaries.reduce((first, next) => next[0].length < first[0].length ? next : first)[0];
      this.send({text: phrase});
      this.text = this.text.slice(phrase.length);
    }
  }
  end() {
    if (this.closed || this.sentAll) return;
    if (this.text) this.send({text: this.text + ' '});
    this.text = '';
    this.sentAll = true;
    this.send({text: ''});
  }
  align({chars, starts, durations}) {
    const valid = Array.isArray(chars) && chars.length && chars.length === starts?.length && chars.length === durations?.length
      && chars.every(char => typeof char === 'string') && [...starts, ...durations].every(value => Number.isFinite(value) && value >= 0);
    if (!valid || !this.alignmentValid) { this.alignmentValid = false; return; }
    let previousStart = this.ends.at(-1)?.start ?? 0, ceiling = this.ends.at(-1)?.ms ?? 0, text = this.alignedText;
    const added = [];
    for (const [index, char] of chars.entries()) {
      if (starts[index] < previousStart - 1) { this.alignmentValid = false; return; }
      previousStart = starts[index];
      ceiling = Math.max(ceiling, starts[index] + durations[index]);
      text += char;
      added.push({offset: text.length, start: previousStart, ms: ceiling});
    }
    if (!this.phrases.join('').startsWith(text)) { this.alignmentValid = false; return; }
    this.alignedText = text;
    this.ends.push(...added);
  }
  provenEnd(offset) {
    let low = 0, high = this.ends.length - 1;
    while (low <= high) {
      const middle = (low + high) >> 1, entry = this.ends[middle];
      if (entry.offset === offset) return entry.ms;
      if (entry.offset < offset) low = middle + 1; else high = middle - 1;
    }
    return null;
  }
  heardPhrases() {
    if (!this.alignmentValid || !this.ends.length) return {count: 0, offset: 0};
    let offset = 0, count = 0;
    for (const phrase of this.phrases) {
      const end = this.provenEnd(offset + phrase.length);
      if (end === null || end + ReadbackPlayer.ALIGNMENT_MARGIN_MS + end * ReadbackPlayer.ALIGNMENT_DRIFT > this.endedMs) break;
      offset += phrase.length;
      count++;
    }
    return {count, offset};
  }
  heardPrefix() {
    const {offset} = this.heardPhrases();
    const text = this.sourceText.slice(0, Math.min(offset, this.sourceText.length));
    return text ? {chars: Array.from(text).length, text, alignment: 'provider'} : null;
  }
  progress() {
    const ids = this.phrases.map((_, index) => 'p' + (index + 1));
    return {status: this.status, text: this.phrases.join(''), phrase_ids: ids,
      aligned_phrase_ids: ids.slice(0, this.heardPhrases().count), heard_prefix: this.heardPrefix(),
      alignment_valid: this.alignmentValid && this.ends.length > 0,
      completed_phrases: this.status === 'completed' ? ids : [], text_complete: this.sentAll,
      audio_scheduled_ms: Math.round(this.scheduledMs), audio_ended_ms: Math.round(this.endedMs)};
  }
  reportHeard() {
    if (this.closed) return;
    const chars = this.heardPrefix()?.chars ?? 0;
    if (chars > this.reportedChars) this.report();
  }
  report() {
    const progress = this.progress();
    this.diagnostic({stage:'progress', ...progress});
    this.reportedChars = progress.heard_prefix?.chars ?? 0;
    try { this.onProgress?.(progress); } catch {}
  }
  track(onProgress) {
    this.onProgress = onProgress;
    this.report();
  }
  finishPlayback() {
    if (!this.closed && this.final && this.sentAll && !this.sources.size) {
      const played = this.started;
      this.close(played ? 'completed' : 'failed');
      this.onEnd();
    }
  }
  fail(error) {
    if (this.closed) return;
    this.close('failed');
    this.onError(error);
  }
  stop() {
    this.close('cancelled');
  }
  close(status) {
    if (this.closed) return;
    this.closed = true;
    this.status = status;
    clearTimeout(this.timeout);
    this.queue = [];
    this.socket.close();
    for (const source of this.sources) { source.onended = null; source.stop(); source.disconnect(); }
    this.sources.clear();
    this.report();
  }
  disconnect() {}
}

ReadbackPlayer.ALIGNMENT_MARGIN_MS = 50;
ReadbackPlayer.ALIGNMENT_DRIFT = .01;

async function readJSONStream(response, onText) {
  if (!response.headers?.get('content-type')?.includes('application/x-ndjson')) return response.json();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = '', result;
  try {
    while (true) {
      const {value, done} = await reader.read();
      pending += decoder.decode(value, {stream: !done});
      const lines = pending.split('\n');
      pending = lines.pop();
      for (const line of lines) {
        if (!line) continue;
        const event = JSON.parse(line);
        if (event.type === 'error') throw new Error(event.error);
        if (event.type === 'text') onText(event.text);
        if (event.type === 'result') result = event.result;
      }
      if (done) break;
    }
    if (!result) throw new Error('Response ended before its outcome was confirmed');
    return result;
  } finally { reader.releaseLock(); }
}
