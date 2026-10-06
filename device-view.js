class DeviceView {
  constructor(storage = localStorage, resume = location.search) {
    this.storage = storage;
    this.generation = 0;
    this.terminals = new Map();
    try { this.saved = JSON.parse(storage.getItem('jev-device-view') || '{}'); } catch { this.saved = {}; }
    this.saved.panes ||= {};
    this.saved.clientId ||= crypto.randomUUID();
    this.selection = this.saved.selection || null;
    const params = new URLSearchParams(resume);
    if (params.get('tab') && params.get('workspace')) {
      this.selection = {tab: params.get('tab'), pane: params.get('pane'), workspace_id: params.get('workspace')};
    }
    this.persist();
  }

  get clientId() { return this.saved.clientId; }

  get query() {
    return this.selection ? '?' + new URLSearchParams({tab: this.selection.tab, ...(this.selection.pane ? {pane: this.selection.pane} : {})}) : '';
  }

  persist() {
    this.saved.selection = this.selection;
    try { this.storage.setItem('jev-device-view', JSON.stringify(this.saved)); } catch {}
  }

  select(state, tabId, paneId = null) {
    const tab = state?.tabs.find(item => item.id === tabId);
    if (!tab) return false;
    const workspace = tab.workspace_id || tab.id;
    const preferred = paneId || this.saved.panes[workspace];
    const pane = tab.panes.find(item => item.id === preferred) || tab.panes.find(item => item.active) || tab.panes[0];
    if (!pane) return false;
    const next = {tab: tab.id, pane: pane.id, workspace_id: tab.workspace_id || null};
    if (JSON.stringify(next) !== JSON.stringify(this.selection)) this.generation += 1;
    this.selection = next;
    this.saved.panes[workspace] = pane.id;
    this.persist();
    return true;
  }

  adopt(state) {
    for (const tab of state.tabs) for (const pane of tab.panes) {
      if (typeof pane.terminal_ansi === 'string') this.terminals.set(JSON.stringify([tab.id, pane.id, pane.identity]), pane.terminal_ansi);
    }
    const savedTab = state.tabs.find(tab => tab.id === this.selection?.tab);
    const sameWorkspace = savedTab && (!this.selection.workspace_id || savedTab.workspace_id === this.selection.workspace_id);
    const tab = sameWorkspace ? savedTab : state.tabs.find(item => item.id === state.selected) || state.tabs[0];
    if (!tab) { this.selection = null; this.persist(); return state; }
    this.select(state, tab.id, sameWorkspace ? this.selection.pane : state.selected_pane);
    return {...state, selected: this.selection.tab, selected_pane: this.selection.pane,
      tabs: state.tabs.map(tab => ({...tab, panes: tab.panes.map(pane => ({...pane,
        active: tab.id === this.selection.tab ? pane.id === this.selection.pane : pane.id === (this.saved.panes[tab.workspace_id || tab.id] || tab.panes.find(item => item.active)?.id),
        terminal_ansi: pane.terminal_ansi ?? this.terminals.get(JSON.stringify([tab.id, pane.id, pane.identity])) ?? '',
      }))}))};
  }
}
