class VoiceControls {
  constructor(submit, target = () => null) {
    this.submit = submit;
    this.target = target;
    this.draft = null;
    this.drafts = new Map();
    this.draftTimer = null;
    this.recognitionTimer = null;
    this.recognitionWaitMs = 2500;
    this.lateWordsTarget = undefined;
    this.graceMs = 3000;
    this.capture = null;
    this.generation = 0;
    this.pending = null;
    this.playing = null;
    this.lastRendition = null;
    this.speechCache = new Map();
    this.listenAfter = 0;
    this.config = {};
    this.loudFrames = 0;
    document.getElementById('voice-toggle').onclick = () => this.capture ? this.stop() : this.start();
    document.addEventListener('visibilitychange', () => { if (document.hidden) this.stop(); });
    window.addEventListener('pagehide', () => this.stop());
    document.getElementById('stop-readback').onclick = () => this.stopReadback('stop and listen');
    document.getElementById('replay-reply').onclick = () => {
      const text = document.getElementById('reply-original').textContent;
      if (text) this.speakReply(text, this.stopReadback('replay reply'));
    };
    try {
      this.drafts = new Map(JSON.parse(sessionStorage.getItem('jev-pane-drafts') || '[]'));
      const saved = sessionStorage.getItem('jev-speech-draft');
      if (saved) {
        this.draft = {...JSON.parse(saved), held: true};
        this.preservePartial();
      }
    } catch {}
    this.renderDraft();
  }
  renderDraft() {
    const draft = this.draft;
    const changed = !!draft && JSON.stringify(draft.target) !== JSON.stringify(this.target());
    document.getElementById('speech-draft-status').textContent = !draft ? '' : draft.held && changed
      ? 'Session changed. Review this draft, then press Send.' : draft.held
      ? draft.unfinalized ? 'Review the last recognized words before sending.' : draft.recoveryReason || ''
      : draft.partial ? 'Listening…' : draft.awaitingCommit ? 'Waiting for recognized words…' : 'Pause to send · keep talking to continue';
    try {
      sessionStorage.setItem('jev-pane-drafts', JSON.stringify([...this.drafts]));
      if (draft) sessionStorage.setItem('jev-speech-draft', JSON.stringify(draft));
      else sessionStorage.removeItem('jev-speech-draft');
    } catch {}
    this.onChange?.();
  }
  selectTarget() {
    const target = this.target();
    const key = value => value ? JSON.stringify([value.tab, value.pane]) : '';
    if (this.draft && key(this.draft.target) !== key(target)) {
      this.stop();
      this.drafts.set(key(this.draft.target), this.draft);
      this.draft = null;
    }
    if (!this.draft && this.drafts.has(key(target))) {
      this.draft = this.drafts.get(key(target));
      this.drafts.delete(key(target));
      this.draft.held = true;
    }
    if (this.draft && JSON.stringify(this.draft.target) !== JSON.stringify(target)) this.holdDraft();
    this.renderDraft();
  }
  speechOnset(recognized = false) {
    clearTimeout(this.draftTimer);
    clearTimeout(this.recognitionTimer);
    this.lastSpeechAt = Date.now();
    if (!this.draft && this.onsetTarget === undefined) this.onsetTarget = this.target();
    if (this.draft) {
      this.draft.awaitingCommit = true;
      if (!recognized && !this.draft.partial) {
        const draft = this.draft;
        this.recognitionTimer = setTimeout(() => {
          if (this.draft !== draft || draft.partial || !draft.awaitingCommit) return;
          draft.awaitingCommit = false;
          draft.recognitionStalled = true;
          draft.recoveryReason = 'No new words recognized yet. More may arrive; review before sending.';
          this.holdDraft();
        }, this.recognitionWaitMs);
      }
      this.renderDraft();
    }
  }
  holdDraft() {
    clearTimeout(this.draftTimer);
    clearTimeout(this.recognitionTimer);
    if (this.draft) this.draft.held = true;
    this.renderDraft();
  }
  restoreDraft(text, target, reason = '') {
    if (this.draft) return false;
    this.draft = {text, partial: '', target, held: true, recoveryReason: reason};
    this.onsetTarget = target;
    this.renderDraft();
    return true;
  }
  preservePartial() {
    if (!this.draft) return;
    if (this.draft.partial) {
      this.draft.text = [this.draft.text, this.draft.partial].filter(Boolean).join(' ');
      this.draft.unfinalized = true;
    }
    this.draft.partial = '';
    this.draft.awaitingCommit = false;
  }
  async finishDraft(manual = false) {
    const draft = this.draft;
    clearTimeout(this.draftTimer);
    if (!draft?.text || draft.partial || draft.awaitingCommit) {
      this.status('Waiting for the final recognized words');
      return;
    }
    if (JSON.stringify(draft.target) !== JSON.stringify(this.target())) {
      const target = this.target();
      if (!manual || !target || target.tab !== draft.target?.tab || target.pane !== draft.target?.pane) {
        this.holdDraft();
        return;
      }
      draft.target = target;
    }
    if (draft.recognitionStalled) this.lateWordsTarget = draft.target;
    this.draft = null;
    this.onsetTarget = undefined;
    this.renderDraft();
    await this.submit(draft.text, draft.target, draft.source || 'voice');
  }
  ensureDraft() {
    if (!this.draft) {
      const late = this.lateWordsTarget !== undefined;
      this.draft = {text: '', partial: '', target: late ? this.lateWordsTarget : this.onsetTarget ?? this.target(), held: late,
        recoveryReason: late ? 'These words may have arrived late. Review before sending.' : ''};
      this.lateWordsTarget = undefined;
    }
  }
  committed(text) {
    clearTimeout(this.recognitionTimer);
    this.ensureDraft();
    this.draft.text = [this.draft.text, text].filter(Boolean).join(' ');
    this.draft.partial = '';
    this.draft.awaitingCommit = false;
    this.draft.recognitionStalled = false;
    clearTimeout(this.draftTimer);
    if (JSON.stringify(this.draft.target) !== JSON.stringify(this.target())) this.draft.held = true;
    this.renderDraft();
    if (!this.draft.held) this.draftTimer = setTimeout(() => this.finishDraft(), this.graceMs);
  }
  status(text) { document.getElementById('voice-status').textContent = text; this.onChange?.(); }
  toggleState(active) {
    const button = document.getElementById('voice-toggle');
    button.setAttribute('aria-pressed', String(active));
    button.setAttribute('aria-label', active ? 'Stop voice input' : 'Start voice input');
    button.title = active ? 'Stop voice input' : 'Start voice input';
  }
  playbackStatus(text) {
    document.getElementById('playback-status').textContent = text;
    this.onChange?.(text);
  }
  recognitionPaused() {
    return ((this.pending || this.playing) && !document.getElementById('talk-to-interrupt').checked) || Date.now() < this.listenAfter;
  }
  log(kind, detail = '') {
    return fetch('/api/voice-event', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({kind, detail})
    }).catch(() => {});
  }
  configure(config) {
    this.config = config;
    document.getElementById('voice-toggle').disabled = !config.configured;
    if (!config.configured && !this.capture) this.status('Voice unavailable');
  }
  stopReadback(reason) {
    this.generation++;
    const wasActive = this.pending || this.playing;
    this.pending?.abort();
    this.pending = null;
    if (this.playing) {
      if (!document.getElementById('talk-to-interrupt').checked) this.listenAfter = Date.now() + 300;
      this.playing.onended = null;
      this.playing.stop();
      this.playing.disconnect();
      this.playing = null;
    }
    document.getElementById('stop-readback').hidden = true;
    if (wasActive) {
      this.log('playback_cancelled', reason);
      this.playbackStatus('Readback stopped · agent unchanged');
    }
    return this.generation;
  }
  async speak(text, generation) {
    const capture = this.capture;
    if (!text || !capture?.ready || generation !== this.generation || this.draft) return;
    const controller = new AbortController();
    this.pending = controller;
    document.getElementById('stop-readback').hidden = false;
    this.playbackStatus(document.getElementById('talk-to-interrupt').checked
      ? 'Preparing audio…' : 'Not listening · preparing audio · Stop & listen to speak');
    try {
      const profile = document.getElementById('voice-profile').value || 'current';
      const key = JSON.stringify([text, profile]);
      let audio = this.speechCache.get(key);
      if (!audio) {
        const response = await fetch('/api/speech', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({text, profile}), signal: controller.signal
        });
        if (!response.ok) throw new Error((await response.json()).error);
        audio = await response.arrayBuffer();
        if (capture !== this.capture || generation !== this.generation) return;
      }
      const buffer = await capture.playback.decodeAudioData(audio.slice(0));
      if (capture !== this.capture || generation !== this.generation) return;
      this.speechCache.set(key, audio);
      if (this.speechCache.size > 3) this.speechCache.delete(this.speechCache.keys().next().value);
      const source = capture.playback.createBufferSource();
      source.buffer = buffer;
      source.connect(capture.playback.destination);
      source.onended = () => {
        if (this.playing !== source) return;
        source.disconnect();
        if (!document.getElementById('talk-to-interrupt').checked) this.listenAfter = Date.now() + 300;
        this.playing = null;
        document.getElementById('stop-readback').hidden = true;
        this.playbackStatus('Readback finished');
        this.log('playback_ended');
      };
      this.playing = source;
      source.start();
      this.playbackStatus(document.getElementById('talk-to-interrupt').checked
        ? 'Speaking · talk to interrupt' : 'Not listening · speaking · Stop & listen to interrupt');
      this.log('playback_started', text);
    } catch (error) {
      if (error.name !== 'AbortError' && generation === this.generation && capture === this.capture) {
        this.playbackStatus('Speech failed: ' + error.message);
        this.log('voice_error', error.message);
      }
    } finally {
      if (this.pending === controller) this.pending = null;
      if (!this.pending && !this.playing) document.getElementById('stop-readback').hidden = true;
      this.onChange?.();
    }
  }
  async speakReply(text, generation) {
    document.getElementById('reply-original').textContent = text;
    document.getElementById('reply-spoken').textContent = '';
    document.getElementById('reply-presentation').hidden = false;
    document.getElementById('replay-reply').hidden = false;
    if (this.draft || !this.capture?.ready) {
      document.getElementById('reply-presentation').open = true;
      this.status(this.draft ? 'Original reply shown · finish your draft before listening.' : 'Reply shown. Turn on voice to listen.');
      return;
    }
    const mode = document.getElementById('speech-style').value || 'auto';
    if (mode === 'original') return this.speak(text, generation);
    const controller = new AbortController();
    this.pending = controller;
    document.getElementById('stop-readback').hidden = false;
    this.playbackStatus(document.getElementById('talk-to-interrupt').checked
      ? 'Preparing readback…' : 'Not listening · preparing readback · Stop & listen to speak');
    try {
      let result = this.lastRendition?.original === text && this.lastRendition.requestedMode === mode
        ? this.lastRendition : null;
      if (!result) {
        const response = await fetch('/api/spoken-version', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({text, mode}), signal: controller.signal
        });
        result = await response.json();
        if (!response.ok) throw new Error(result.error);
      }
      if (generation !== this.generation) return;
      this.lastRendition = {...result, original: text, requestedMode: mode};
      if (result.mode === 'uncertain') {
        document.getElementById('reply-spoken').textContent = 'Jev could not choose a readback style. Choose Original wording or Conversational rendition to listen.';
        document.getElementById('reply-presentation').open = true;
        this.playbackStatus('Readback paused · choose a style');
        return;
      }
      document.getElementById('reply-spoken').textContent = result.mode === 'original'
        ? 'Reading original wording.' : result.text;
      await this.speak(result.text, generation);
    } catch (error) {
      if (error.name !== 'AbortError' && generation === this.generation) {
        document.getElementById('reply-spoken').textContent = 'Readback unavailable. The original remains below; choose Original wording to listen.';
        document.getElementById('reply-presentation').open = true;
        this.playbackStatus('Could not prepare readback');
      }
    } finally {
      if (this.pending === controller) this.pending = null;
      if (!this.pending && !this.playing) document.getElementById('stop-readback').hidden = true;
      this.onChange?.();
    }
  }
  async start() {
    const capture = {};
    this.capture = capture;
    this.toggleState(true);
    this.status('Opening microphone…');
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('Microphone requires localhost or HTTPS');
      capture.context = new AudioContext({sampleRate: 16000});
      await capture.context.resume();
      if (this.capture !== capture) return;
      capture.playback = new AudioContext();
      await capture.playback.resume();
      if (this.capture !== capture) return;
      const stream = await navigator.mediaDevices.getUserMedia({audio: {
        channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true
      }});
      if (this.capture !== capture) { stream.getTracks().forEach(track => track.stop()); return; }
      capture.stream = stream;
      if (capture.context.sampleRate !== 16000) throw new Error('This browser cannot capture at 16 kHz');
      await capture.context.audioWorklet.addModule('/microphone.js');
      if (this.capture !== capture) return;
      capture.input = capture.context.createMediaStreamSource(stream);
      capture.processor = new AudioWorkletNode(capture.context, 'microphone');
      capture.input.connect(capture.processor);
      capture.processor.connect(capture.context.destination);
      capture.ws = new WebSocket(this.config.websocket_url);
      capture.timer = setTimeout(() => this.fail(capture, 'Speech connection timed out'), 15000);
      capture.processor.port.onmessage = ({data}) => {
        if (capture !== this.capture || !capture.ready) return;
        const paused = this.recognitionPaused();
        this.loudFrames = !paused && data.rms > 0.025 ? this.loudFrames + 1 : 0;
        if (this.loudFrames === 2) {
          this.speechOnset();
          this.stopReadback('microphone speech onset');
          this.log('speech_detected');
        }
        if (capture.ws.readyState === WebSocket.OPEN) {
          if (capture.ws.bufferedAmount > 64000) { this.fail(capture, 'Audio connection is too slow'); return; }
          const bytes = paused ? new Uint8Array(data.pcm.byteLength) : new Uint8Array(data.pcm.buffer);
          capture.ws.send(JSON.stringify({message_type: 'input_audio_chunk',
            audio_base_64: btoa(String.fromCharCode(...bytes))}));
        }
      };
      capture.ws.onmessage = ({data}) => {
        if (capture !== this.capture) return;
        const message = JSON.parse(data);
        if (message.message_type === 'session_started') {
          clearTimeout(capture.timer);
          capture.ready = true;
          this.status('Listening · pause about 4 seconds to send');
          this.log('mic_started');
        } else if (message.message_type === 'partial_transcript') {
          if (!this.recognitionPaused() && message.text?.trim()) {
            this.speechOnset(true);
            this.ensureDraft();
            this.draft.partial = message.text;
            this.renderDraft();
            this.stopReadback('recognized speech');
          }
        } else if (message.message_type === 'committed_transcript' && !this.recognitionPaused() && message.text?.trim()) {
          document.getElementById('transcript').textContent = '';
          this.committed(message.text);
        } else if (message.message_type === 'error') this.fail(capture, message.error);
      };
      capture.ws.onerror = () => this.fail(capture, 'Local speech relay connection failed');
      capture.ws.onclose = () => this.fail(capture, 'Speech connection closed; enable voice to reconnect');
      stream.getTracks().forEach(track => { track.onended = () => this.fail(capture, 'Microphone disconnected'); });
    } catch (error) {
      this.fail(capture, error.name === 'NotAllowedError' ? 'Microphone permission denied' : error.message);
    }
  }
  fail(capture, message) {
    if (capture !== this.capture) return;
    this.stop();
    this.status(message);
    this.log('voice_error', message);
  }
  stop() {
    this.preservePartial();
    this.holdDraft();
    const capture = this.capture;
    this.capture = null;
    this.lateWordsTarget = undefined;
    this.onsetTarget = undefined;
    this.stopReadback('voice disabled');
    if (capture) {
      clearTimeout(capture.timer);
      capture.ws?.close();
      capture.stream?.getTracks().forEach(track => track.stop());
      capture.input?.disconnect();
      capture.processor?.disconnect();
      capture.context?.close().catch(() => {});
      capture.playback?.close().catch(() => {});
      this.log('mic_stopped');
    }
    this.loudFrames = 0;
    document.getElementById('transcript').textContent = '';
    this.toggleState(false);
    this.status('Microphone off');
  }
}
