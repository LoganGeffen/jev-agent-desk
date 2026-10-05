class LocalWakeDetector {
  static async create() {
    const runtime = await import('/wake-assets/runtime.js');
    const manifest = await (await fetch('/wake-assets/hey_jarvis.json')).json();
    const pipeline = new runtime.InferencePipeline();
    try {
      const info = await pipeline.load({wakeWordModel: location.origin + '/wake-assets/hey_jarvis.tflite',
        wasmPath: location.origin + '/wake-assets/wasm/', sampleRate: 16000});
      return new LocalWakeDetector(pipeline, info.frameSize,
        new runtime.Detector({threshold: manifest.micro.probability_cutoff,
          windowSize: manifest.micro.sliding_window_size, refractoryMs: 2000}));
    } catch (error) {
      pipeline.dispose();
      throw error;
    }
  }
  constructor(pipeline, frameSize, detector) {
    this.pipeline = pipeline;
    this.frame = new Float32Array(frameSize);
    this.offset = 0;
    this.detector = detector;
    this.closed = false;
  }
  feed(pcm) {
    if (this.closed) return false;
    let fired = false;
    for (const sample of pcm) {
      this.frame[this.offset++] = sample / 32768;
      if (this.offset === this.frame.length) {
        fired = this.detector.push(this.pipeline.process(this.frame)).fired || fired;
        this.offset = 0;
      }
    }
    return fired;
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.pipeline.dispose();
  }
}
