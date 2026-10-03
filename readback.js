class ReadbackPlayer {
  constructor(url, context, {onStart, onEnd, onError, onProgress = null}) {
    this.context = context;
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
    this.charEnds = [];
    this.alignedText = '';
    this.alignmentValid = true;
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
        if (message.alignment) this.align(message.alignment);
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
            source.disconnect();
            this.finishPlayback();
          };
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
    const last = this.charEnds.at(-1) ?? 0;
    if (starts[0] < last - 1) { this.alignmentValid = false; return; }
    chars.forEach((char, index) => { this.alignedText += char; this.charEnds.push(starts[index] + durations[index]); });
    if (!this.phrases.join('').startsWith(this.alignedText)) this.alignmentValid = false;
  }
  alignedPhraseIds() {
    if (!this.alignmentValid) return [];
    const ids = [];
    let offset = 0;
    for (const [index, phrase] of this.phrases.entries()) {
      offset += phrase.length;
      if (offset > this.charEnds.length || this.charEnds[offset - 1] + ReadbackPlayer.ALIGNMENT_MARGIN_MS > this.endedMs) break;
      ids.push('p' + (index + 1));
    }
    return ids;
  }
  progress() {
    const ids = this.phrases.map((_, index) => 'p' + (index + 1));
    return {status: this.status, text: this.phrases.join(''), phrase_ids: ids,
      aligned_phrase_ids: this.alignedPhraseIds(), alignment_valid: this.alignmentValid && this.charEnds.length > 0,
      completed_phrases: this.status === 'completed' ? ids : [], text_complete: this.sentAll,
      audio_scheduled_ms: Math.round(this.scheduledMs), audio_ended_ms: Math.round(this.endedMs)};
  }
  report() {
    try { this.onProgress?.(this.progress()); } catch {}
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
