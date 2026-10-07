class PushNotificationClient {
  constructor({fetch, storage, serviceWorker, NotificationApi, location, history, onChange = () => {}}) {
    this.fetch = fetch;
    this.storage = storage;
    this.serviceWorker = serviceWorker;
    this.NotificationApi = NotificationApi;
    this.location = location;
    this.history = history;
    this.onChange = onChange;
    this.sessionId = null;
    this.target = null;
    this.publicKey = null;
    this.signature = null;
    this.registration = null;
    this.linkHandled = false;
    this.view = {supported: Boolean(serviceWorker && NotificationApi), ready: false, enabled: false, muted: false,
      busy: false, message: serviceWorker && NotificationApi ? 'Notifications are off on this device.' : 'Web Push is unavailable in this browser.',
      linkMessage: ''};
    let deviceId = null;
    try { deviceId = storage.getItem('jev-push-device-id'); } catch {}
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(deviceId || '')) {
      deviceId = crypto.randomUUID();
      try { storage.setItem('jev-push-device-id', deviceId); } catch {}
    }
    this.deviceId = deviceId;
    const params = new URLSearchParams(location.search);
    const fields = ['notification', 'tab', 'pane', 'identity'];
    if (params.has('notification')) {
      this.link = fields.every(key => params.getAll(key).length === 1 && params.get(key))
        ? {event_id: params.get('notification'), target: {tab: params.get('tab'), pane: params.get('pane'), identity: params.get('identity')}}
        : {invalid: true};
    } else this.link = null;
    if (this.view.supported) {
      this.registrationPromise = serviceWorker.register('/service-worker.js').then(registration => {
        this.registration = registration;
        return registration;
      }).catch(error => {
        this.view.supported = false;
        this.view.message = 'Web Push setup failed: ' + error.message;
        this.emit();
        return null;
      });
    }
  }

  emit() { this.onChange({...this.view, canMute: Boolean(this.view.enabled && this.target)}); }

  async post(path, body) {
    const response = await this.fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: this.sessionId, device_id: this.deviceId, ...body})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Notification request failed');
    return result;
  }

  async refreshState() {
    const signature = this.signature;
    const target = this.target;
    const state = await this.post('/api/push/state', target ? {target} : {});
    if (signature !== this.signature) return;
    this.publicKey = state.public_key;
    this.view.ready = typeof state.public_key === 'string' && Boolean(state.public_key);
    this.view.enabled = state.enabled;
    this.view.muted = state.muted;
    this.view.message = state.enabled ? 'Notifications are enabled on this device.' : 'Notifications are off on this device.';
  }

  async adopt(sessionId, target) {
    if (!sessionId || !this.view.supported) return;
    if (sessionId !== this.sessionId) this.view.ready = false;
    this.sessionId = sessionId;
    this.target = target;
    const signature = JSON.stringify([sessionId, target]);
    if (signature === this.signature) return;
    this.signature = signature;
    try {
      await this.refreshState();
    } catch (error) {
      if (signature === this.signature) this.view.message = error.message;
    }
    this.emit();
  }

  async enable() {
    if (!this.sessionId || !this.view.supported || !this.view.ready || this.view.busy) return;
    this.view.busy = true;
    this.view.message = 'Enabling notifications…';
    this.emit();
    try {
      const permission = await this.NotificationApi.requestPermission();
      if (permission !== 'granted') throw new Error('Notification permission was not granted.');
      const registration = this.registration || await this.registrationPromise;
      if (!registration?.pushManager) throw new Error('Push subscriptions are unavailable in this browser.');
      const subscription = await registration.pushManager.getSubscription() || await registration.pushManager.subscribe({
        userVisibleOnly: true, applicationServerKey: PushNotificationClient.decodeKey(this.publicKey)
      });
      const serialized = subscription.toJSON();
      await this.post('/api/push/subscribe', {subscription: {endpoint: serialized.endpoint, keys: serialized.keys}});
      this.view.enabled = true;
      this.view.message = 'Notifications are enabled on this device.';
    } catch (error) {
      this.view.message = error.message;
    } finally {
      this.view.busy = false;
      this.emit();
    }
  }

  async test() {
    if (!this.sessionId || !this.view.enabled || this.view.busy) return;
    this.view.busy = true;
    this.view.message = 'Queueing a test notification…';
    this.emit();
    try {
      const result = await this.post('/api/push/test', {});
      this.view.message = `Test queued for ${result.delay_seconds} seconds. Lock this device now.`;
    } catch (error) {
      try { await this.refreshState(); } catch {}
      this.view.message = error.message;
    } finally {
      this.view.busy = false;
      this.emit();
    }
  }

  async disable() {
    if (!this.sessionId || this.view.busy) return;
    this.view.busy = true;
    this.view.message = 'Disabling notifications…';
    this.emit();
    try {
      await this.post('/api/push/unsubscribe', {});
      const registration = this.registration || await this.registrationPromise;
      const subscription = await registration?.pushManager?.getSubscription();
      if (subscription) await subscription.unsubscribe();
      this.view.enabled = false;
      this.view.muted = false;
      this.view.message = 'Notifications are off on this device.';
    } catch (error) {
      this.view.message = error.message;
    } finally {
      this.view.busy = false;
      this.emit();
    }
  }

  async setMuted(muted) {
    if (!this.sessionId || !this.target || !this.view.enabled || this.view.busy) return;
    this.view.busy = true;
    this.emit();
    try {
      const result = await this.post('/api/push/mute', {target: this.target, muted});
      this.view.muted = result.muted;
      this.view.message = result.muted ? 'This conversation is muted on this device.' : 'This conversation can notify this device.';
    } catch (error) {
      this.view.message = error.message;
    } finally {
      this.view.busy = false;
      this.emit();
    }
  }

  async resolveLink(state, navigate) {
    if (this.linkHandled || !this.link || !this.sessionId) return;
    this.linkHandled = true;
    let exact = false;
    if (!this.link.invalid) {
      const tab = state.tabs.find(item => item.id === this.link.target.tab);
      const pane = tab?.panes.find(item => item.id === this.link.target.pane);
      exact = pane?.identity === this.link.target.identity;
    }
    try {
      if (this.link.invalid) throw new Error('The notification link is invalid.');
      await this.post('/api/push/open', {event_id: this.link.event_id, target: this.link.target,
        status: exact ? 'opened' : 'stale'});
      if (!exact) throw new Error('That notification conversation is unavailable or changed. No other conversation was opened.');
      await navigate(this.link.target.tab, this.link.target.pane);
      this.view.linkMessage = 'Opened the exact conversation from the notification.';
    } catch (error) {
      this.view.linkMessage = error.message.includes('unavailable or changed') ? error.message
        : 'That notification conversation is unavailable or changed. No other conversation was opened.';
    } finally {
      this.history.replaceState(null, '', this.location.pathname || '/');
      this.emit();
    }
  }

  static decodeKey(value) {
    const padding = '='.repeat((4 - value.length % 4) % 4);
    const bytes = atob((value + padding).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(bytes, character => character.charCodeAt(0));
  }
}

if (typeof module !== 'undefined') module.exports = {PushNotificationClient};
