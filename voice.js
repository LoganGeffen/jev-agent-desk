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
    this.semantic = null;
    this.onDiscard = null;
    this.silenceMs = 700;
    this.recheckMs = 4000;
    this.maxRechecks = 2;
    this.evalSeq = 0;
    this.evaluating = null;
    this.newId = () => globalThis.crypto?.randomUUID?.() ?? 'draft-' + Date.now() + '-' + Math.random().toString(36).slice(2);
    this.capture = null;
    this.generation = 0;
    this.pending = null;
    this.playing = null;
    this.lastRendition = null;
    this.speechCache = new Map();
    this.listenAfter = 0;
    this.config = {};
    this.wakeIdleMs = 60000;
    this.loudFrames = 0;
    document.getElementById('voice-toggle').onclick = () => this.capture ? this.stop() : this.start();
    this.localWake = true;
    this.autoAttempted = false;
    document.addEventListener('visibilitychange', () => { if (document.hidden) this.stop(); });
    window.addEventListener('pagehide', () => this.stop());
    document.getElementById('stop-readback').onclick = () => this.stopReadback('stop and listen');
    document.getElementById('replay-reply').onclick = () => {
      const text = document.getElementById('reply-original').textContent;
      if (text) this.speakReply(text, this.stopReadback('replay reply'), false, this.replyHeard, this.replyTrack);
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
    const changed = !!draft && (this.draftContextChanged(draft) || JSON.stringify(draft.target) !== JSON.stringify(this.target()));
    document.getElementById('speech-draft-status').textContent = !draft ? '' : draft.held && changed
      ? 'Session changed. Review this draft, then press Send.' : draft.held
      ? draft.unfinalized ? 'Review the last recognized words before sending.' : draft.recoveryReason || ''
      : draft.partial ? 'Listening…' : draft.awaitingCommit ? 'Waiting for recognized words…'
      : draft.checking ? 'Checking whether you are finished…'
      : draft.waiting ? 'Waiting for you · keep talking or press Send'
      : this.semantic ? 'Keep talking, or pause and Jarvis decides when you are done' : 'Pause to send · keep talking to continue';
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
  cancelEvaluation() {
    this.evalSeq++;
    this.evaluating?.abort();
    this.evaluating = null;
    if (this.draft) { this.draft.checking = false; this.draft.waiting = false; }
  }
  identify(draft) {
    draft.transcriptId ||= this.newId();
    draft.revision ??= 0;
    return draft;
  }
  touchTranscript() {
    this.cancelEvaluation();
    const draft = this.draft && this.identify(this.draft);
    if (!draft) return;
    draft.revision++;
    if (draft.outstanding) {
      draft.outstanding = false;
      this.semantic?.invalidate({transcript_id: draft.transcriptId, transcript_revision: draft.revision, text: draft.text});
    }
  }
  speechOnset(recognized = false) {
    if (this.capture?.wake && !this.draft && !this.capture.firstRequest) {
      this.capture.requestContext = this.wakeContext(); this.capture.firstRequest = true;
    }
    if (this.capture?.wake) this.wakeActivity(this.capture);
    clearTimeout(this.draftTimer);
    clearTimeout(this.recognitionTimer);
    this.touchTranscript();
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
    this.cancelEvaluation();
    if (this.draft) { this.draft.held = true; this.draft.wakeResume = false; }
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
  wakeContext() { return JSON.stringify(this.semantic?.context?.() ?? null); }
  draftContextChanged(draft) { return draft?.wakeContext !== undefined && draft.wakeContext !== this.wakeContext(); }
  async finishDraft(manual = false, decision = null) {
    const draft = this.draft;
    clearTimeout(this.draftTimer);
    this.cancelEvaluation();
    if (!draft?.text || draft.partial || draft.awaitingCommit) {
      this.status('Waiting for the final recognized words');
      return;
    }
    if (!manual && this.draftContextChanged(draft)) { this.holdDraft(); return; }
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
    const capture = this.capture;
    if (capture?.wake) { capture.requestsPending = (capture.requestsPending || 0) + 1; this.wakeActivity(capture); }
    this.trace('transcript_dispatch', {text: draft.text, transcript_id: draft.transcriptId, transcript_revision: draft.revision, target: draft.target});
    const transcript = {transcript_id: this.identify(draft).transcriptId, transcript_revision: draft.revision};
    try {
      await this.submit(draft.text, draft.target, draft.source || 'voice',
        decision ? {...transcript, decision_id: decision.decision_id} : {...transcript, explicit_send: manual});
    } finally {
      if (capture?.wake) { capture.requestsPending--; this.wakeActivity(capture); }
    }
  }
  async evaluate(attempt = 0) {
    const draft = this.draft;
    if (!this.semantic || !draft?.text || draft.partial || draft.awaitingCommit || draft.held) return;
    if (this.draftContextChanged(draft) || JSON.stringify(draft.target) !== JSON.stringify(this.target())) return this.holdDraft();
    this.evaluating?.abort();
    const seq = ++this.evalSeq, text = draft.text, revision = ++this.identify(draft).revision;
    const controller = this.evaluating = new AbortController();
    draft.outstanding = true;
    draft.checking = true;
    draft.waiting = false;
    this.renderDraft();
    let result = null;
    try {
      result = await this.semantic.decide({transcript_id: draft.transcriptId, transcript_revision: revision, text}, controller.signal);
    } catch { result = null; }
    if (seq !== this.evalSeq || this.draft !== draft || draft.revision !== revision || draft.text !== text) return;
    this.evaluating = null;
    draft.checking = false;
    if (result?.decision === 'complete' && typeof result.decision_id === 'string' && result.decision_id) {
      return this.finishDraft(false, result);
    }
    if (result?.decision === 'cancel') return this.discardDraft(draft);
    draft.waiting = true;
    this.renderDraft();
    if (attempt < this.maxRechecks) {
      this.draftTimer = setTimeout(() => this.evaluate(attempt + 1), this.recheckMs * 2 ** attempt);
    }
  }
  discardDraft(draft) {
    clearTimeout(this.draftTimer);
    this.cancelEvaluation();
    this.draft = null;
    this.onsetTarget = undefined;
    this.renderDraft();
    if (this.capture?.wake) this.waitForWake(this.capture);
    else this.status('Discarded unsent words');
    this.onDiscard?.(draft);
  }
  ensureDraft() {
    if (!this.draft) {
      const late = this.lateWordsTarget !== undefined;
      this.draft = {text: '', partial: '', target: late ? this.lateWordsTarget : this.onsetTarget ?? this.target(), held: late,
        transcriptId: this.newId(), revision: 0, outstanding: false,
        ...(this.capture?.wake ? {wakeContext: this.capture.firstRequest ? this.capture.requestContext : this.wakeContext()} : {}),
        recoveryReason: late ? 'These words may have arrived late. Review before sending.' : ''};
      this.lateWordsTarget = undefined;
    }
  }
  committed(text) {
    if (this.capture?.wake) this.wakeActivity(this.capture);
    clearTimeout(this.recognitionTimer);
    this.ensureDraft();
    if (this.capture?.wake) this.capture.firstRequest = false;
    this.touchTranscript();
    this.draft.text = [this.draft.text, text].filter(Boolean).join(' ');
    this.draft.partial = '';
    this.draft.awaitingCommit = false;
    this.draft.recognitionStalled = false;
    clearTimeout(this.draftTimer);
    if (this.draftContextChanged(this.draft) || JSON.stringify(this.draft.target) !== JSON.stringify(this.target())) this.draft.held = true;
    this.renderDraft();
    if (this.draft.held) return;
    this.draftTimer = this.semantic ? setTimeout(() => this.evaluate(), this.silenceMs)
      : setTimeout(() => this.finishDraft(), this.graceMs);
  }
  status(text) { document.getElementById('voice-status').textContent = text; this.trace('activation', {text}); this.onChange?.(); }
  toggleState(active) {
    const button = document.getElementById('voice-toggle');
    button.setAttribute('aria-pressed', String(active));
    button.setAttribute('aria-label', active ? 'Stop Jarvis' : this.localWake ? 'Enable Jarvis' : 'Start voice input');
    button.title = active ? 'Stop Jarvis' : this.localWake ? 'Enable Jarvis' : 'Start voice input';
    const label = document.getElementById('voice-toggle-label');
    if (label) label.textContent = button.title;
  }
  playbackStatus(text) {
    if (this.capture?.wake && this.capture.wakeState === 'listening') this.wakeActivity(this.capture);
    document.getElementById('playback-status').textContent = text;
    this.onChange?.(text);
  }
  recognitionPaused() {
    return ((this.pending || this.playing) && !document.getElementById('talk-to-interrupt').checked) || Date.now() < this.listenAfter;
  }
  trace(stage, data = {}) {
    try { return this.log('pipeline', {stage, capture_id: this.capture?.id, generation: this.generation,
      transcript_id: this.draft?.transcriptId, transcript_revision: this.draft?.revision,
      context: this.semantic?.context?.(), ...data}); } catch { return Promise.resolve(); }
  }
  log(kind, detail = '') {
    try { return fetch('/api/voice-event', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({kind, detail})
    }).catch(() => {}); } catch { return Promise.resolve(); }
  }
  configure(config) {
    this.config = config;
    document.getElementById('voice-toggle').disabled = !config.configured;
    if (!config.configured && !this.capture) this.status('Voice unavailable');
    if (config.configured && !this.autoAttempted && !document.hidden) {
      this.autoAttempted = true;
      const generation = this.generation;
      this.automaticallyEnable(generation);
    }
  }
  async automaticallyEnable(generation) {
    let permission;
    try { permission = await navigator.permissions?.query({name: 'microphone'}); } catch {}
    if (this.capture || this.generation !== generation || document.hidden) return;
    if (permission?.state === 'denied') { this.status('Microphone permission denied · Enable Jarvis after allowing access'); return; }
    await this.start({automatic: true});
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
  playerRevoked(player) {
    if (this.playing !== player) return;
    this.playing = null;
    if (!document.getElementById('talk-to-interrupt').checked) this.listenAfter = Date.now() + 300;
    if (!this.pending) document.getElementById('stop-readback').hidden = true;
    this.log('playback_cancelled', 'playback taken over');
    this.playbackStatus('Readback stopped · playing on another device · agent unchanged');
    this.onChange?.();
  }
  async speak(text, generation, onHeard = null) {
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
        onHeard?.();
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
  beginReadback(generation, onHeard = null, track = null, style = 'original') {
    const capture = this.capture;
    if (!capture?.ready || this.draft || generation !== this.generation) return null;
    const profile = document.getElementById('voice-profile').value || 'current';
    const url = this.config.websocket_url + (this.config.websocket_url.includes('?') ? '&' : '?') + 'tts=' + encodeURIComponent(profile) + '&capture_id=' + encodeURIComponent(capture.id) + '&generation=' + generation;
    const started = Date.now();
    const finish = error => {
      if (this.playing !== player) return;
      this.playing = null;
      if (!document.getElementById('talk-to-interrupt').checked) this.listenAfter = Date.now() + 300;
      document.getElementById('stop-readback').hidden = true;
      this.playbackStatus(error ? 'Speech failed: ' + error.message : 'Readback finished');
      this.log(error ? 'voice_error' : 'playback_ended', error?.message || '');
      if (!error && !track && player.started && generation === this.generation && capture === this.capture) onHeard?.();
    };
    const player = new ReadbackPlayer(url, capture.playback, {
      onStart: () => {
        if (generation !== this.generation || capture !== this.capture) return player.stop();
        this.playbackStatus('Speaking · Stop & listen to interrupt');
        this.log('playback_started', JSON.stringify({first_audio_ms: Date.now() - started, streaming: true}));
      },
      onEnd: () => finish(), onError: finish,
      onDiagnostic: data => this.trace('audio_playback', {generation, ...data}),
    });
    this.playing = player;
    document.getElementById('stop-readback').hidden = false;
    this.playbackStatus('Preparing audio…');
    try { track?.(player, {style, generation}); } catch {}
    return player;
  }
  presentReply(text) {
    this.replyHeard = null;
    this.replyTrack = null;
    document.getElementById('reply-original').textContent = text;
    document.getElementById('reply-spoken').textContent = '';
    document.getElementById('reply-presentation').hidden = false;
    document.getElementById('replay-reply').hidden = false;
  }
  async speakReply(text, generation, spokenReady = false, onHeard = null, track = null) {
    if (generation !== this.generation) return;
    this.trace('readback_source', {generation, original:text, turn_id:track?.turnId});
    this.presentReply(text);
    this.replyHeard = onHeard;
    this.replyTrack = track;
    if (this.draft || !this.capture?.ready) {
      document.getElementById('reply-presentation').open = true;
      this.status(this.draft ? 'Original reply shown · finish your draft before listening.' : 'Reply shown. Turn on voice to listen.');
      return;
    }
    const mode = spokenReady ? 'original' : document.getElementById('speech-style').value || 'spoken';
    if (mode === 'original') {
      const player = this.beginReadback(generation, onHeard, track, 'original');
      player?.write(text); player?.end();
      return;
    }
    const controller = new AbortController();
    this.pending = controller;
    document.getElementById('stop-readback').hidden = false;
    this.playbackStatus('Preparing readback…');
    let player = null, bound = null, tracker = track;
    try {
      if (typeof track?.prepare === 'function') {
        bound = await track.prepare({style: 'spoken', generation});
        tracker = bound?.track ?? null;
        if (controller.signal.aborted || generation !== this.generation) return;
      }
      let result = !bound && this.lastRendition?.original === text && this.lastRendition.requestedMode === mode
        ? this.lastRendition : null;
      if (!result) {
        const response = await fetch('/api/spoken-version', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({text, mode, stream: true, ...bound?.fields}), signal: controller.signal
        });
        result = await readJSONStream(response, delta => {
          if (generation !== this.generation) return;
          player ||= this.beginReadback(generation, onHeard, tracker, 'spoken');
          player?.write(delta);
        });
        if (!response.ok) throw new Error(result.error);
      }
      if (generation !== this.generation) return;
      this.lastRendition = {...result, original: text, requestedMode: mode};
      document.getElementById('reply-spoken').textContent = result.mode === 'original'
        ? 'Reading original wording.' : result.text;
      if (!player) { player = this.beginReadback(generation, onHeard, tracker, 'spoken'); player?.write(result.text); }
      player?.end();
    } catch (error) {
      if (error.name !== 'AbortError' && generation === this.generation) {
        if (player) { player.stop(); if (this.playing === player) this.playing = null; }
        document.getElementById('reply-spoken').textContent = 'Readback unavailable. The original remains below; choose Original wording to listen.';
        document.getElementById('reply-presentation').open = true;
        this.playbackStatus('Could not prepare readback');
      }
    } finally {
      bound?.cancel();
      if (this.pending === controller) this.pending = null;
      if (!this.pending && !this.playing) document.getElementById('stop-readback').hidden = true;
      this.onChange?.();
    }
  }
  async start({automatic = false} = {}) {
    this.autoAttempted = true;
    const capture = {id: globalThis.crypto?.randomUUID?.() ?? 'capture-' + Date.now() + '-' + Math.random().toString(36).slice(2)};
    this.capture = capture;
    this.toggleState(true);
    this.status('Opening microphone…');
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('Microphone requires localhost or HTTPS');
      capture.context = new AudioContext({sampleRate: 16000});
      if (!automatic) await capture.context.resume();
      if (this.capture !== capture) return;
      capture.playback = new AudioContext();
      if (!automatic) await capture.playback.resume();
      if (this.capture !== capture) return;
      const stream = await navigator.mediaDevices.getUserMedia({audio: {
        channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true
      }});
      if (this.capture !== capture) { stream.getTracks().forEach(track => track.stop()); return; }
      capture.stream = stream;
      stream.getTracks().forEach(track => { track.onended = () => this.fail(capture, 'Microphone disconnected'); });
      if (automatic) {
        await Promise.race([Promise.all([capture.context.resume(), capture.playback.resume()]),
          new Promise((_, reject) => {
            capture.abortActivation = () => reject(new Error('Enable Jarvis once to activate audio'));
            capture.activationTimer = setTimeout(capture.abortActivation, 1500);
          })]);
        clearTimeout(capture.activationTimer);
        capture.abortActivation = null;
        if (this.capture !== capture) return;
        if (capture.context.state !== 'running' || capture.playback.state !== 'running')
          throw new Error('Enable Jarvis once to activate audio');
      }
      if (capture.context.sampleRate !== 16000) throw new Error('This browser cannot capture at 16 kHz');
      await capture.context.audioWorklet.addModule('/microphone.js');
      if (this.capture !== capture) return;
      capture.input = capture.context.createMediaStreamSource(stream);
      capture.processor = new AudioWorkletNode(capture.context, 'microphone');
      capture.input.connect(capture.processor);
      capture.processor.connect(capture.context.destination);
      capture.wakeMode = this.localWake;
      capture.processor.port.onmessage = ({data}) => this.microphonePacket(capture, data);
      if (capture.wakeMode) {
        this.status('Loading on-device Hey Jarvis…');
        const wake = await LocalWakeDetector.create();
        if (this.capture !== capture) { wake.close(); return; }
        capture.wake = wake;
        capture.ready = true;
        this.waitForWake(capture);
      } else this.openRecognition(capture);
    } catch (error) {
      this.fail(capture, error.name === 'NotAllowedError' ? 'Microphone permission denied' : error.message);
    }
  }
  microphonePacket(capture, data) {
    if (capture !== this.capture || (!capture.ready && !capture.wakeMode)) return;
    try {
      if (capture.wake) {
        const wasPlayback = !!(this.pending || this.playing);
        if (this.recognitionPaused()) {
          capture.playbackBuffer ||= [];
          capture.playbackSamples = (capture.playbackSamples || 0) + data.pcm.length;
          capture.playbackBuffer.push(data.pcm.slice());
          while (capture.playbackSamples > 48000) capture.playbackSamples -= capture.playbackBuffer.shift().length;
        }
        const detected = capture.wake.feed(data.pcm);
        if (detected && capture.wakeState === 'listening') {
          this.stopReadback('Hey Jarvis'); this.listenAfter = 0; this.wakeActivity(capture);
          if (wasPlayback) {
            capture.prefixConsumed = false; capture.boundaryPending = '';
            for (const pcm of capture.playbackBuffer || []) this.sendAudio(capture, pcm);
            capture.playbackBuffer = []; capture.playbackSamples = 0;
            return;
          }
        }
        if (capture.wakeState === 'waiting' || capture.wakeState === 'connecting') {
          capture.buffer.push(data.pcm.slice());
          capture.bufferSamples += data.pcm.length;
          if (capture.wakeState === 'waiting') {
            while (capture.bufferSamples > 48000) capture.bufferSamples -= capture.buffer.shift().length;
            if (detected) this.wakeDetected(capture);
          } else if (capture.bufferSamples > 128000) this.fail(capture, 'Speech connection too slow; command audio was not sent');
          return;
        }
      }
      if (!capture.cloudReady) return;
      const paused = this.recognitionPaused();
      this.loudFrames = !paused && data.rms > 0.025 ? this.loudFrames + 1 : 0;
      if (this.loudFrames === 2) {
        this.speechOnset();
        this.stopReadback('microphone speech onset');
        this.log('speech_detected');
      }
      if (capture.ws.bufferedAmount > 64000) { this.fail(capture, 'Audio connection is too slow'); return; }
      this.sendAudio(capture, paused ? new Int16Array(data.pcm.length) : data.pcm);
    } catch (error) { this.fail(capture, error.message); }
  }
  sendAudio(capture, pcm) {
    if (capture !== this.capture || !capture.cloudReady || capture.ws?.readyState !== WebSocket.OPEN) return;
    const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
    capture.ws.send(JSON.stringify({message_type: 'input_audio_chunk', audio_base_64: btoa(String.fromCharCode(...bytes))}));
  }
  wakeDetected(capture) {
    if (capture !== this.capture || capture.wakeState !== 'waiting') return;
    this.stopReadback('Hey Jarvis');
    const resume = this.draft?.wakeResume && !this.draft.unfinalized && !this.draftContextChanged(this.draft) &&
      JSON.stringify(this.draft.target) === JSON.stringify(this.target());
    if (this.draft && !resume) {
      this.holdDraft();
      this.status('Review or discard the retained speech draft before a new wake request');
      return;
    }
    if (resume) { this.draft.held = false; this.draft.wakeResume = false; }
    this.listenAfter = 0;
    this.onsetTarget = this.draft?.target ?? this.target();
    capture.requestContext = this.draft?.wakeContext ?? this.wakeContext();
    capture.firstRequest = true;
    capture.wakeState = 'connecting';
    capture.prefixConsumed = false;
    capture.boundaryPending = '';
    this.status('Hey Jarvis heard · opening request recognition…');
    const cue = capture.playback.createOscillator?.();
    if (cue) {
      const gain = capture.playback.createGain();
      gain.gain.value = 0.05;
      cue.frequency.value = 660;
      cue.connect(gain); gain.connect(capture.playback.destination);
      cue.onended = () => { cue.disconnect(); gain.disconnect(); };
      cue.start(); cue.stop(capture.playback.currentTime + 0.08);
    }
    this.openRecognition(capture);
    this.wakeActivity(capture);
  }
  wakeText(capture, text, committed) {
    this.wakeActivity(capture);
    if (capture.prefixConsumed) return text.replace(/^\s*hey[\s,]+jarvis\b[\s.,!?;:]*/i, '');
    const pending = capture.boundaryPending || '';
    const joined = pending ? pending + ' ' + text : text;
    const prefix = /(?:^|\b)hey[\s,]+jarvis\b[\s.,!?;:]*/i.exec(joined);
    if (prefix) {
      if (committed) { capture.prefixConsumed = true; capture.boundaryPending = ''; }
      return text.slice(Math.max(0, prefix.index + prefix[0].length - (pending ? pending.length + 1 : 0)));
    }
    if (committed) capture.boundaryPending = /(?:^|\b)hey[\s,]*$/i.test(text) ? 'Hey' : '';
    this.status('Wake detected locally · waiting for recognized Hey Jarvis boundary');
    return '';
  }
  wakeActivity(capture) {
    if (capture !== this.capture || capture.wakeState === 'waiting') return;
    clearTimeout(capture.wakeIdle);
    const epoch = capture.wakeEpoch;
    capture.wakeIdle = setTimeout(() => this.wakeTimeout(capture, epoch), this.wakeIdleMs);
  }
  wakeTimeout(capture, epoch) {
    if (capture !== this.capture || capture.wakeState === 'waiting' || capture.wakeEpoch !== epoch) return;
    if (capture.requestsPending || this.pending || this.playing) { this.wakeActivity(capture); return; }
    const resumable = this.draft && !this.draft.held && !this.draft.partial && !this.draft.unfinalized;
    this.preservePartial();
    this.holdDraft();
    if (resumable) this.draft.wakeResume = true;
    this.waitForWake(capture);
    if (resumable) this.status('Request paused locally · say Hey Jarvis to continue the same request');
  }
  waitForWake(capture) {
    if (capture !== this.capture || !capture.wake) return;
    clearTimeout(capture.timer);
    clearTimeout(capture.wakeIdle);
    clearTimeout(capture.wakeMaximum);
    const socket = capture.ws;
    capture.ws = null;
    if (socket) { socket.onmessage = socket.onerror = socket.onclose = null; socket.close(); }
    capture.cloudReady = false;
    capture.wakeEpoch = (capture.wakeEpoch || 0) + 1;
    capture.wakeState = 'waiting';
    capture.buffer = [];
    capture.bufferSamples = 0;
    capture.playbackBuffer = []; capture.playbackSamples = 0;
    this.loudFrames = 0;
    this.status('Dormant · listening on-device for Hey Jarvis');
  }
  openRecognition(capture) {
    if (capture !== this.capture) return;
    const socket = capture.ws = new WebSocket(this.config.websocket_url + (this.config.websocket_url.includes('?') ? '&' : '?')
      + 'capture_id=' + encodeURIComponent(capture.id) + '&socket_epoch=' + (capture.wakeEpoch || 0));
    capture.cloudReady = false;
    capture.timer = setTimeout(() => this.fail(capture, 'Speech connection timed out'), 15000);
    capture.ws.onmessage = ({data}) => {
      if (capture !== this.capture || capture.ws !== socket) return;
      const message = JSON.parse(data);
      if (message.text) this.trace('recognition', {socket_epoch: capture.wakeEpoch, message_type: message.message_type, text: message.text});
      if (capture.wake && this.draft?.held && message.text) {
        this.status('Review or discard the retained draft before continuing'); return;
      }
      if (capture.wake && message.text) message.text = this.wakeText(capture, message.text, message.message_type === 'committed_transcript');
      if (message.message_type === 'session_started') {
        clearTimeout(capture.timer);
        capture.ready = true;
        capture.cloudReady = true;
        if (capture.wake) {
          capture.wakeState = 'listening';
          for (const pcm of capture.buffer) this.sendAudio(capture, pcm);
          capture.buffer = [];
          capture.bufferSamples = 0;
          this.wakeActivity(capture);
        }
        this.status(this.semantic ? 'Listening · Jarvis decides when you are done' : 'Listening · pause about 4 seconds to send');
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
    capture.ws.onerror = () => { if (capture.ws === socket) this.fail(capture, 'Local speech relay connection failed'); };
    capture.ws.onclose = () => { if (capture.ws === socket) this.fail(capture, 'Speech connection closed; enable voice to reconnect'); };
  }
  fail(capture, message) {
    if (capture !== this.capture) return;
    this.stop();
    this.status(message);
    this.log('voice_error', message);
  }
  stop() {
    this.autoAttempted = true;
    this.preservePartial();
    this.holdDraft();
    const capture = this.capture;
    this.capture = null;
    this.lateWordsTarget = undefined;
    this.onsetTarget = undefined;
    this.stopReadback('voice disabled');
    if (capture) {
      capture.abortActivation?.();
      clearTimeout(capture.activationTimer);
      clearTimeout(capture.timer);
      clearTimeout(capture.wakeIdle);
      clearTimeout(capture.wakeMaximum);
      capture.wake?.close();
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
