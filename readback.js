class ReadbackPlayer {
  constructor(url, context, {onStart, onEnd, onError}) {
    this.context = context;
    this.sources = new Set();
    this.queue = [];
    this.text = '';
    this.nextTime = 0;
    this.closed = false;
    this.final = false;
    this.started = false;
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
            source.disconnect();
            this.finishPlayback();
          };
          this.sources.add(source);
          this.nextTime = Math.max(context.currentTime + .03, this.nextTime);
          source.start(this.nextTime);
          this.nextTime += buffer.duration;
          if (!this.started) { this.started = true; onStart(); }
        }
        if (message.isFinal) { this.final = true; clearTimeout(this.timeout); this.finishPlayback(); }
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
    const text = JSON.stringify(message);
    if (this.socket.readyState === WebSocket.OPEN) this.socket.send(text);
    else this.queue.push(text);
  }
  write(delta) {
    if (this.closed) return;
    this.text += delta;
    let sentence;
    while ((sentence = this.text.match(/^[\s\S]*?[.!?]["')\]]*\s+/))) {
      this.send({text: sentence[0]});
      this.text = this.text.slice(sentence[0].length);
    }
  }
  end() {
    if (this.text) this.send({text: this.text + ' '});
    this.text = '';
    this.send({text: ''});
  }
  finishPlayback() {
    if (!this.closed && this.final && !this.sources.size) {
      this.stop();
      this.onEnd();
    }
  }
  fail(error) {
    if (this.closed) return;
    this.stop();
    this.onError(error);
  }
  stop() {
    if (this.closed) return;
    this.closed = true;
    clearTimeout(this.timeout);
    this.queue = [];
    this.socket.close();
    for (const source of this.sources) { source.onended = null; source.stop(); source.disconnect(); }
    this.sources.clear();
  }
  disconnect() {}
}

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
