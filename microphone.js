class Microphone extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Int16Array(2048);
    this.offset = 0;
    this.energy = 0;
  }
  process(inputs) {
    const samples = inputs[0]?.[0];
    if (!samples) return true;
    for (const sample of samples) {
      this.energy += sample * sample;
      this.buffer[this.offset++] = Math.max(-1, Math.min(1, sample)) * 32767;
      if (this.offset === this.buffer.length) {
        this.port.postMessage({pcm: this.buffer, rms: Math.sqrt(this.energy / this.offset)});
        this.offset = 0;
        this.energy = 0;
      }
    }
    return true;
  }
}
registerProcessor('microphone', Microphone);
