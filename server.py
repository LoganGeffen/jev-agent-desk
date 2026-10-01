import argparse
import copy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shlex
import string
import threading
import time
import uuid

import jev
from attention import AttentionQueue
from codex_actions import focus, tmux
from session_actions import interrupt_turn, read_reply, send_message
from claude_runtime import read_binding
from claude_actions import working_indicator as claude_working_indicator
from session_questions import ask_session
from message_delivery import prepare_message
from spoken_reply import ReadbackCache
from reply_watch import ReadbackPreparation, ReplyWatch, history as reply_history
from voice import Voice, load_voice_settings
from pane_identity import process_identity


ATTENTION_OFF = {"", "0", "false", "no", "off"}
NEEDS_YOU = {"blocked", "needs_input", "needs_you", "waiting_user", "action_required"}
WORKING = {"busy", "executing", "launching", "queued", "running", "thinking", "working"}
READY = {"complete", "completed", "done", "ready", "waiting"}
TERMINAL_KEYS = {
    "Enter", "Escape", "BSpace", "Tab", "BTab", "Up", "Down", "Left", "Right",
    "Home", "End", "DC", "IC", "PPage", "NPage",
    *(f"F{number}" for number in range(1, 13)),
    *(f"S-{key}" for key in ("Up", "Down", "Left", "Right", "Home", "End")),
    *(f"M-{key}" for key in ("Up", "Down", "Left", "Right", "Home", "End")),
    *(f"C-{key}" for key in ("Up", "Down", "Left", "Right", "Home", "End")),
    *(f"C-{character}" for character in string.ascii_lowercase + string.digits + "@[]\\^_/?"),
    *(f"M-{character}" for character in string.ascii_letters + string.digits + "@[]\\^_/?"),
    "C-Space", "M-Space",
}


def activity_state(status, attention, command):
    normalized_status = status.strip().lower().replace("-", "_").replace(" ", "_")
    if attention.strip().lower() not in ATTENTION_OFF or normalized_status in NEEDS_YOU:
        return "needs_you"
    if normalized_status in WORKING:
        return "working"
    if normalized_status in READY:
        return "ready"
    if normalized_status == "idle" or command.strip().lower() in {"claude", "codex"}:
        return "idle"
    return "open"


def clean_name(value, label):
    if not isinstance(value, str):
        raise ValueError(f"A {label} name is required")
    name = value.strip()
    if not name or len(name) > 80 or any(ord(character) < 32 for character in name):
        raise ValueError(f"Use a {label} name between 1 and 80 visible characters")
    return name


def latest_event(path):
    path = Path(path)
    if not path.exists():
        return None
    latest = None
    for line in path.read_text().splitlines():
        if line.strip():
            latest = json.loads(line)
    return latest


class TabGroups:
    DEFAULTS = ({"id": "workspace", "name": "Workspace"},
                {"id": "agents", "name": "Agents"})

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def _read(self, tabs):
        if self.path.exists():
            stored = json.loads(self.path.read_text())
            if not isinstance(stored, dict):
                raise ValueError("Tab group state must be an object")
            raw_groups = stored.get("groups")
            raw_placements = stored.get("placements", [])
        else:
            raw_groups = self.DEFAULTS
            raw_placements = []
        if not isinstance(raw_groups, (list, tuple)) or not raw_groups:
            raise ValueError("At least one tab group is required")
        groups = []
        group_ids = set()
        for raw in raw_groups:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                raise ValueError("Each tab group needs an id and name")
            group_id = raw["id"]
            if not group_id or group_id in group_ids:
                raise ValueError("Tab group ids must be nonempty and unique")
            groups.append({"id": group_id, "name": clean_name(raw.get("name"), "group")})
            group_ids.add(group_id)
        if not isinstance(raw_placements, list):
            raise ValueError("Tab placements must be a list")
        live = {tab["id"]: tab for tab in tabs}
        placements = []
        placed = set()
        for raw in raw_placements:
            if not isinstance(raw, dict):
                continue
            tab_id, group_id = raw.get("tab"), raw.get("group")
            if tab_id in live and group_id in group_ids and tab_id not in placed:
                placements.append({"tab": tab_id, "group": group_id})
                placed.add(tab_id)
        fallback = groups[0]["id"]
        for tab in tabs:
            if tab["id"] in placed:
                continue
            preferred = "agents" if tab.get('agent', tab['codex']) else "workspace"
            placements.append({"tab": tab["id"],
                               "group": preferred if preferred in group_ids else fallback})
        return {"version": 1, "groups": groups, "placements": placements}

    def _write(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n")
        temporary.replace(self.path)

    def view(self, tabs):
        state = self._read(tabs)
        by_id = {tab["id"]: tab for tab in tabs}
        ordered = []
        for group in state["groups"]:
            for placement in state["placements"]:
                if placement["group"] != group["id"]:
                    continue
                tab = dict(by_id[placement["tab"]])
                tab.update(group_id=group["id"], group=group["name"])
                ordered.append(tab)
        return ordered, state["groups"]

    def create(self, name, tabs):
        name = clean_name(name, "group")
        with self.lock:
            state = self._read(tabs)
            group_id = "group-" + uuid.uuid4().hex[:12]
            state["groups"].append({"id": group_id, "name": name})
            self._write(state)
        return group_id

    def rename(self, group_id, name, tabs):
        name = clean_name(name, "group")
        with self.lock:
            state = self._read(tabs)
            group = next((group for group in state["groups"] if group["id"] == group_id), None)
            if group is None:
                raise ValueError("That tab group is no longer available")
            group["name"] = name
            self._write(state)

    def move(self, tab_id, group_id, before, tabs):
        with self.lock:
            state = self._read(tabs)
            if tab_id not in {tab["id"] for tab in tabs}:
                raise ValueError("That tab is no longer available")
            if group_id not in {group["id"] for group in state["groups"]}:
                raise ValueError("That tab group is no longer available")
            if before == tab_id:
                return
            placements = [placement for placement in state["placements"]
                          if placement["tab"] != tab_id]
            placement = {"tab": tab_id, "group": group_id}
            if before is not None:
                index = next((index for index, item in enumerate(placements)
                              if item["tab"] == before and item["group"] == group_id), None)
                if index is None:
                    raise ValueError("The requested tab position is no longer available")
                placements.insert(index, placement)
            else:
                indices = [index for index, item in enumerate(placements)
                           if item["group"] == group_id]
                placements.insert(indices[-1] + 1 if indices else len(placements), placement)
            state["placements"] = placements
            self._write(state)


class Playground:
    def __init__(self, socket, log, evaluate=jev.evaluate, source_session="tabs",
                 view_session=None, redact_terminal_logs=False, independent_selection=False):
        self.socket = socket
        self.log = Path(log)
        self.evaluate = evaluate
        self.source_session = source_session
        self.view_session = view_session or source_session
        self.redact_terminal_logs = redact_terminal_logs
        self.independent_selection = independent_selection
        self.selected_window = None
        self.selected_panes = {}
        source_group = self.session_group(self.source_session)
        view_group = self.session_group(self.view_session)
        if source_group != view_group:
            raise ValueError("The source and view sessions do not share a tmux session group")
        self.latest = latest_event(self.log)
        self.recent_requests = {}
        self.readbacks = ReadbackCache()
        self.reply_watch = ReplyWatch()
        self.attention = AttentionQueue()
        self.readback_preparation = ReadbackPreparation(self.readbacks, self.attention)
        self.read_output = None
        self.pending_close = None
        self.pending_clarification = None
        self.lock = threading.Lock()
        self.input_lock = threading.Lock()
        self.groups = TabGroups(self.log.parent / "groups.json")
        self.voice = Voice(self.log.parent / "voice.jsonl")

    def session_group(self, session):
        name, group = tmux(self.socket, "display-message", "-p", "-t", f"={session}:",
                           "#{session_name}\t#{session_group}").split("\t", 1)
        if name != session:
            raise ValueError(f"Tmux session {session!r} is not available")
        return group or name

    def readback_agents(self):
        windows = tmux(self.socket, 'list-windows', '-t', f'={self.source_session}', '-F', '#{window_id}')
        return [{'socket': self.socket, 'window_id': window, 'pane_id': pane['id'],
                 'provider': pane['provider'], 'thread_id': pane['thread_id'],
                 'process': pane['process'], 'run_dir': str(self.log.parent)}
                for window in windows.splitlines() for pane in self.panes(window) if pane['agent']]

    def observe(self):
        rows = tmux(self.socket, "list-windows", "-t", f"={self.view_session}", "-F",
                    "#{window_id}\t#{window_index}\t#{window_name}\t#{window_active}"
                    "\t#{window_activity}\t#{@agent_status}\t#{@agent_attention}"
                    "\t#{pane_current_command}")
        tabs = []
        selected = None
        for row in rows.splitlines():
            window_id, index, name, active, activity, status, attention, command = row.split("\t")
            panes = self.panes(window_id)
            codex = any(pane['codex'] for pane in panes)
            state = activity_state(status, attention, command)
            claude_states = {pane.get('state') for pane in panes if pane.get('provider') == 'claude'}
            if claude_states:
                state = next((value for value in ('needs_you', 'working', 'ready', 'idle') if value in claude_states), state)
            tabs.append({"id": window_id, "index": int(index), "name": name,
                         "codex": codex, 'agent': any(pane.get('agent') or pane['codex'] for pane in panes), "panes": panes,
                         "state": state,
                         "activity": int(activity or 0)})
            if active == "1":
                selected = window_id
        if self.independent_selection:
            if self.selected_window not in {tab["id"] for tab in tabs}:
                self.selected_window = selected
            selected = self.selected_window
        tabs, groups = self.groups.view(tabs)
        visible = next(tab for tab in tabs if tab['id'] == selected)['panes']
        for pane in visible:
            pane['terminal_ansi'] = tmux(self.socket, 'capture-pane', '-p', '-e', '-S', '-', '-t', pane['id'])
        active = next(pane for pane in visible if pane['active'])
        terminal_ansi = active['terminal_ansi']
        terminal = re.sub(r"\x1b\[[0-9;:]*m", "", terminal_ansi)
        return {"tabs": tabs, "groups": groups, "selected": selected,
                "selected_pane": active['id'],
                "terminal": terminal, "terminal_ansi": terminal_ansi}

    def panes(self, window):
        fields = ('pane_id', 'pane_index', 'pane_active', 'pane_pid', 'pane_current_command',
                  'pane_current_path', 'pane_left', 'pane_top', 'pane_width', 'pane_height',
                  '@agent_handoff_id', '@agent_handoff_role', '@agent_handoff_peer', 'pane_title')
        rows = tmux(self.socket, 'list-panes', '-t', f'{self.view_session}:{window}', '-F',
                    '\t'.join('#{' + field + '}' for field in fields))
        panes = []
        for row in rows.splitlines():
            values = dict(zip(fields, row.split('\t'), strict=True))
            identity = process_identity(values['pane_pid'], title=values['pane_title'])
            command = values['pane_current_command']
            thread_id = identity['thread_id'] if command == 'codex' else None
            binding = read_binding(self.log.parent, identity['process']) if command == 'claude' else None
            if binding:
                thread_id = binding['session_id']
            state = None
            if binding:
                screen = tmux(self.socket, 'capture-pane', '-p', '-t', values['pane_id'])
                state = ('working' if claude_working_indicator(screen) else 'needs_you' if binding['state'] == 'needs_you'
                         else 'ready' if binding.get('last_reply') else 'idle')
            panes.append({
                'id': values['pane_id'], 'number': len(panes) + 1,
                'active': values['pane_active'] == '1', 'command': command, 'title': values['pane_title'],
                'workspace': values['pane_current_path'], 'thread_id': thread_id,
                'codex': command == 'codex' and bool(thread_id), 'agent': bool(thread_id),
                'provider': command if command in ('codex', 'claude') else None,
                'state': state,
                'process': identity['process'],
                'tracking': ('lifecycle_hooks' if binding else 'unresolved') if command == 'claude'
                            else identity.get('tracking', 'unresolved') if command == 'codex' else None,
                'identity': f"{identity['process']}:{command}:{thread_id or ''}",
                'handoff': {key: values['@agent_handoff_' + key] for key in ('id', 'role', 'peer')},
                **{key: int(values['pane_' + key]) for key in ('left', 'top', 'width', 'height')},
            })
        if self.independent_selection:
            selected = self.selected_panes.get(window)
            if selected not in {pane["id"] for pane in panes}:
                selected = next(pane["id"] for pane in panes if pane["active"])
                self.selected_panes[window] = selected
            for pane in panes:
                pane["active"] = pane["id"] == selected
        return panes

    def focus(self, window, pane=None):
        if self.independent_selection:
            self.selected_window = window
            if pane is not None:
                self.selected_panes[window] = pane
        else:
            focus(self.socket, window, pane, self.view_session)

    def validate_pane(self, window, pane, identity=None):
        windows = tmux(self.socket, 'list-windows', '-t', f'={self.source_session}',
                       '-F', '#{window_id}').splitlines()
        if window not in windows:
            raise ValueError('That tab is no longer available')
        live = next((item for item in self.panes(window) if item['id'] == pane), None)
        if live is None or (identity is not None and live['identity'] != identity):
            raise ValueError('That pane or conversation changed; refresh and try again')
        return live

    def state(self):
        return {**self.observe(), "latest": self.latest, "read_output": self.read_output,
                'pending_close': self.pending_close,
                'pending_clarification': self.pending_clarification,
                'request_results': list(self.recent_requests.values()),
                'reply_notices': self.reply_watch.view(),
                'target': {'source_session': self.source_session,
                           'view_session': self.view_session},
                "voice": self.voice.config(),
                "api_configured": bool(os.environ.get("TYPESAFE_API_KEY"))}

    def submit(self, request, source="typed", request_id=None, capture_target=None, on_text=None,
               attention_target=None):
        with self.lock:
            self.pending_close = None
            started = time.monotonic()
            event = {"timestamp": datetime.now(timezone.utc).isoformat(), "request": request,
                     "source": source, "request_id": request_id,
                     "input": None, "response": None, "action": None, "target": None,
                     "before": None, "after": None, "outcome": "error", "error": None,
                     "delivery": "not_attempted"}
            try:
                before = self.observe()
                event["before"] = before
                if capture_target:
                    self.validate_pane(capture_target['tab'], capture_target['pane'], capture_target['identity'])
                    if before['selected'] != capture_target['tab'] or before['selected_pane'] != capture_target['pane']:
                        raise ValueError('Selection changed during composition. Return to the original pane and review the retained text.')
                if self.pending_clarification:
                    pending = self.pending_clarification
                    event['input'] = jev.clarification_question(request, pending)
                    event['response'] = self.evaluate(event['input'])
                    resolution = event['response']['answers']['resolution']['choice']
                    if resolution != 'new_request':
                        return self._resolve_clarification(pending['id'], resolution, event)
                    self.pending_clarification = None
                routing_tabs = [
                    {**{key: tab[key] for key in ("id", "index", "name", "codex", 'agent')},
                     'agent': any(pane['command'] in ('codex', 'claude') or pane.get('agent') for pane in tab['panes']),
                     'panes': [{**{key: pane[key] for key in
                                ('id', 'number', 'active', 'command', 'codex', 'thread_id', 'handoff')},
                                'agent': bool(pane['command'] in ('codex', 'claude') or pane.get('agent')),
                                'identity_ready': bool(pane['thread_id']), 'provider': pane.get('provider')}
                               for pane in tab['panes']]}
                    for tab in before["tabs"]
                ]
                event["input"] = jev.question(request, routing_tabs, before["selected"])
                if attention_target is not None:
                    event['input']['state']['attention_target'] = attention_target
                event["routing_steps"] = []
                response = jev.interpret(event["input"], self.evaluate, event["routing_steps"])
                event["response"] = response
                event['boundary_refinement'] = jev.refine_boundaries(event['input'], response, self.evaluate)
                action = event["action"] = response["answers"]["action"]["choice"]
                if action not in event['input']['questions']['action']['criteria']:
                    raise ValueError('Jev selected an action outside this request envelope')
                tabs = {tab["id"]: tab for tab in before["tabs"]}

                def target_pane():
                    target = response['answers']['target']['choice']
                    if target not in tabs:
                        raise ValueError('No recipient was resolved. Your words were not sent.')
                    tab = tabs[target]
                    pane_id = response['answers'][f'pane:{target}']['choice']
                    pane = next((pane for pane in tab['panes'] if pane['id'] == pane_id), None)
                    if pane is None:
                        raise ValueError('No matching pane in the requested tab')
                    self.validate_pane(target, pane_id, pane['identity'])
                    event.update(target=target, pane=pane_id,
                                 target_label=f"{tab['name']} · pane {pane['number']}")
                    return tab, pane

                def select():
                    tab, pane = target_pane()
                    self.focus(tab['id'], pane['id'])

                def no_action():
                    target = response['answers'].get('target', {}).get('choice')
                    tab = tabs.get(target)
                    if tab and not tab.get('agent') and any(
                            pane['command'] in ('codex', 'claude') for pane in tab['panes']):
                        event['output'] = (f"No action taken. {tab['name']}'s conversation identity is unavailable. "
                                           "Your words are saved; nothing was sent.")

                def close():
                    target = response['answers']['target']['choice']
                    tab = tabs[target]
                    scope = response['answers']['close_scope']['choice'] if action == 'clarify_close' else (
                        'tab' if action == 'close_tab' else 'pane')
                    if scope not in ('tab', 'pane'):
                        raise ValueError('Unknown shutdown scope')
                    if scope == 'tab':
                        event.update(target=target, target_label=tab['name'])
                        identities = {pane['id']: pane['identity'] for pane in tab['panes']}
                        pane_id = None
                        label = f"tab {tab['name']} and all {len(tab['panes'])} pane(s)"
                        closing = tab['panes']
                    else:
                        _, pane = target_pane()
                        identities = {pane['id']: pane['identity']}
                        pane_id = pane['id']
                        label = f"{tab['name']}, pane {pane['number']}"
                        if pane['handoff']['role']:
                            label += f" ({pane['handoff']['role']})"
                        closing = [pane]
                    if any(pane['command'] in ('codex', 'claude') and not pane['thread_id'] for pane in closing):
                        raise ValueError('Agent conversation identity is unresolved; use the manual pane control or relaunch with identity tracking')
                    proposal = {'id': uuid.uuid4().hex, 'action': 'close_' + scope, 'tab': target,
                                'pane': pane_id, 'identities': identities, 'label': label}
                    if action == 'clarify_close':
                        _, recipient = target_pane()
                        message = jev.message_text(request, event['input'], response, 'clarification') if recipient.get('agent') or recipient['codex'] else None
                        self.pending_clarification = {
                            'id': uuid.uuid4().hex, 'request': request, 'message': message,
                            'tab': target, 'pane': recipient, 'recipient': event['target_label'],
                            'close': proposal,
                        }
                        event['clarification_required'] = True
                        event['output'] = self.clarification_text(self.pending_clarification)
                    else:
                        self.pending_close = proposal
                        event['confirmation_required'] = True
                        event['output'] = f'Confirm shutting down {label}. Its running processes will end.'

                def agent_action():
                    tab, pane = target_pane()
                    target = tab['id']
                    if not (pane.get('agent') or pane['codex']):
                        raise ValueError(f"{tab['name']}'s conversation is not available yet. Your words are saved.")
                    agent = {'socket': self.socket, 'window_id': target, 'pane_id': pane['id'],
                             'view_session': self.view_session,
                             'preserve_focus': self.independent_selection,
                             'provider': pane.get('provider') or 'codex', 'run_dir': str(self.log.parent),
                             **{key: pane[key] for key in ('thread_id', 'process', 'workspace')}}
                    if self.independent_selection and action in ('read_reply', 'interrupt_turn'):
                        self.focus(target, pane["id"])
                    event["execution"] = {"agent": agent}
                    if action == "send_message":
                        text = jev.message_text(request, event["input"], response)
                        delivery = prepare_message(request, text, response["answers"]["message_form"]["choice"], tabs[target]["name"])
                        event["execution"].update(delivery)
                        with self.input_lock:
                            self.deliver(agent, delivery['text'], event)
                    elif action == "interrupt_turn":
                        event["output"] = interrupt_turn(self.socket, agent)
                    elif action == "ask_session":
                        result = ask_session(self.socket, agent, request, **({'on_text': on_text} if on_text else {}))
                        event["execution"]["observation"] = result
                        event["output"] = result["text"]
                        self.read_output = {"target": event['target_label'], "text": event["output"],
                                            "kind": "session answer", "observed_at": result["snapshot"]["observed_at"]}
                    else:
                        event["output"] = read_reply(self.socket, agent)
                        self.read_output = {"target": event['target_label'], "text": event["output"]}
                        item = next((item for item in self.attention.view(self.observe()['tabs'], include_heard=True)
                                     if item['tab'] == target and item['pane'] == pane['id']
                                     and item['text'] == event['output']), None)
                        if item:
                            event['heard_reply'] = self.attention.reference(item)

                {**{name: lambda: self.attention_action(action, attention_target, event)
                    for name in jev.ATTENTION_ACTIONS},
                 "select_tab": select, "list_tabs": lambda: None,
                 "create_tab": lambda: self.create_tab('Terminal', next(tab['group_id'] for tab in before['tabs'] if tab['id'] == before['selected'])),
                 "close_tab": close, "close_pane": close, 'clarify_close': close,
                 "send_message": agent_action, "read_reply": agent_action,
                 "interrupt_turn": agent_action, "ask_session": agent_action,
                 "no_action": no_action}[action]()
                event["outcome"] = "ok"
            except Exception as error:
                event["error"] = f"{type(error).__name__}: {error}"
                if isinstance(error, jev.ProviderError):
                    event["provider_error"] = error.provider_error
            try:
                event["after"] = self.observe()
            except Exception as error:
                event['observation_error'] = str(error)
            event["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            self.record_event(event)
            return event

    def attention_action(self, action, reference, event):
        snapshot = self.observe()
        if action in ('recommend_next', 'list_ready'):
            items = self.attention.view(snapshot['tabs'], include_heard=True)
            self.attention.classify(items, self.evaluate)
            items = self.attention.view(self.observe()['tabs'])
            event['recommendation'] = self.attention.reference(items[0]) if items else None
            if not items:
                event['output'] = 'No chats are waiting for your input or have unread replies right now.'
            elif action == 'recommend_next':
                first = items[0]
                reason = 'it needs your input' if first['needs_input'] else 'it has the oldest unread reply'
                event['output'] = f"{len(items)} chat{'s' if len(items) != 1 else ''} ready. Start with {first['label']}; {reason}."
            else:
                event['output'] = 'Ready chats: ' + '; '.join(
                    item['label'] + (' needs your input' if item['needs_input'] else ' has an unread reply')
                    for item in items) + '.'
            return
        if not isinstance(reference, dict):
            raise ValueError('Ask what is next first so I know which chat you mean.')
        item = next((item for item in self.attention.view(snapshot['tabs'], include_heard=True)
                     if self.attention.reference(item) == reference), None)
        if item is None:
            raise ValueError('That recommendation changed or is no longer available. Ask what is next again.')
        self.validate_pane(item['tab'], item['pane'], item['identity'])
        event.update(target=item['tab'], pane=item['pane'], target_label=item['label'])
        if action == 'select_recommended':
            self.focus(item['tab'], item['pane'])
            event['output'] = 'Switched to ' + item['label'] + '.'
        else:
            if not item['text']:
                raise ValueError('That agent needs your input but has no completed reply to read. Say go there to inspect it.')
            event['output'] = item['text']
            event['heard_reply'] = self.attention.reference(item)
            self.read_output = {'target': item['label'], 'text': item['text']}
            self.focus(item['tab'], item['pane'])
        event['recommendation'] = reference

    def heard_reply(self, reference):
        if 'notice_id' in reference:
            notice = next((item for item in self.reply_watch.view() if item['id'] == reference['notice_id']), None)
            if not notice:
                return
            self.validate_pane(notice['tab'], notice['pane'], notice['identity'])
            item = next((item for item in self.attention.view(self.observe()['tabs'], include_heard=True)
                         if all(item.get(key) == notice.get(key) for key in ('tab', 'pane', 'identity', 'turn_id', 'text'))), None)
            if item:
                self.attention.heard(item['id'])
            return
        self.validate_pane(reference['tab'], reference['pane'], reference['identity'])
        item = next((item for item in self.attention.view(self.observe()['tabs'], include_heard=True)
                     if all(item[key] == reference[key] for key in ('id', 'tab', 'pane', 'identity'))), None)
        if item:
            self.attention.heard(item['id'])

    def deliver(self, agent, text, event):
        baseline = None
        if event.get('source') in ('typed', 'voice', 'manual_clarification'):
            try:
                baseline = {turn['id'] for turn in reply_history(agent)['turns']}
            except (RuntimeError, OSError, ValueError) as error:
                event['reply_watch_error'] = str(error)
        event['delivery'] = 'uncertain'
        send_message(self.socket, agent, text)
        event['delivery'] = 'submitted'
        self.attention.answered(agent)
        if baseline is not None:
            self.reply_watch.start(agent, baseline, text, event['target_label'], event.get('request_id'))

    def record_event(self, event):
        stored = self.event_for_log(event) if self.redact_terminal_logs else event
        with self.log.open('a') as stream:
            stream.write(json.dumps(stored) + '\n')
        self.latest = event
        if event.get('request_id'):
            self.recent_requests[event['request_id']] = {
                key: event[key] for key in ('request_id', 'timestamp', 'outcome', 'action',
                                          'target', 'target_label', 'delivery', 'error', 'output', 'reply_watch_error',
                                          'recommendation', 'heard_reply') if key in event}
            self.recent_requests[event['request_id']]['delivered_text'] = event.get('execution', {}).get('text')
            while len(self.recent_requests) > 50:
                self.recent_requests.pop(next(iter(self.recent_requests)))

    @staticmethod
    def event_for_log(event):
        stored = copy.deepcopy(event)
        for key in ('before', 'after'):
            snapshot = stored.get(key)
            if not isinstance(snapshot, dict):
                continue
            snapshot.pop('terminal', None)
            snapshot.pop('terminal_ansi', None)
            for tab in snapshot.get('tabs', []):
                for pane in tab.get('panes', []):
                    pane.pop('terminal_ansi', None)
        execution = stored.get('execution')
        observation = execution.get('observation') if isinstance(execution, dict) else None
        if isinstance(observation, dict):
            execution['observation'] = {
                key: observation[key] for key in (
                    'observed_at', 'thread_id', 'provider', 'live_working_indicator',
                    'history_truncated', 'history_error', 'history_missing', 'capture_ms'
                ) if key in observation
            }
        return stored

    @staticmethod
    def clarification_text(pending):
        if pending['message'] is None:
            return f"Shut down {pending['close']['label']}? This pane cannot receive agent messages."
        return (f"Send {pending['message']!r} to {pending['recipient']}, "
                f"or shut down {pending['close']['label']}?")

    def _resolve_clarification(self, request_id, choice, event):
        pending = self.pending_clarification
        if pending is None or pending['id'] != request_id:
            raise ValueError('That clarification is no longer pending')
        if choice not in ('send', 'shutdown', 'cancel', 'unclear'):
            raise ValueError('Unknown clarification choice')
        event.setdefault('delivery', 'not_attempted')
        event.update(clarification_id=request_id, original_request=pending['request'],
                     target=pending['tab'], pane=pending['pane']['id'],
                     target_label=pending['recipient'], outcome='ok', error=None)
        if choice != 'unclear':
            self.pending_clarification = None
        try:
            if choice == 'send':
                event['action'] = 'send_message'
                pane = pending['pane']
                if pending['message'] is None:
                    raise ValueError('This pane cannot receive agent messages')
                agent = {'socket': self.socket, 'window_id': pending['tab'], 'pane_id': pane['id'],
                         'view_session': self.view_session,
                             'preserve_focus': self.independent_selection,
                         'provider': pane.get('provider') or 'codex', 'run_dir': str(self.log.parent),
                         **{key: pane[key] for key in ('thread_id', 'process', 'workspace')}}
                event['execution'] = {'agent': agent, 'text': pending['message'],
                                      'source_text': pending['message'], 'form': 'verbatim'}
                with self.input_lock:
                    self.validate_pane(pending['tab'], pane['id'], pane['identity'])
                    if self.independent_selection:
                        self.focus(pending['tab'], pane['id'])
                    self.deliver(agent, pending['message'], event)
            elif choice == 'shutdown':
                proposal = pending['close']
                event.update(action=proposal['action'], pane=proposal['pane'], target_label=proposal['label'])
                if proposal['action'] == 'close_tab':
                    live = {p['id']: p['identity'] for p in self.panes(proposal['tab'])}
                    if live != proposal['identities']:
                        raise ValueError('The tab panes changed; ask again')
                for pane, identity in proposal['identities'].items():
                    self.validate_pane(proposal['tab'], pane, identity)
                self.pending_close = proposal
                event['confirmation_required'] = True
                event['output'] = f"Confirm shutting down {proposal['label']}. Its running processes will end."
            elif choice == 'cancel':
                event['action'] = 'cancel_clarification'
            else:
                event.update(action='clarify_close', clarification_required=True,
                             output=self.clarification_text(pending))
        except Exception as error:
            event.update(outcome='error', error=f'{type(error).__name__}: {error}')
        try:
            event['after'] = self.observe()
        except Exception as error:
            event['observation_error'] = str(error)
        self.record_event(event)
        return event

    def resolve_clarification(self, request_id, choice):
        with self.lock:
            event = {'timestamp': datetime.now(timezone.utc).isoformat(), 'source': 'manual_clarification',
                     'request': choice, 'before': self.observe()}
            result = self._resolve_clarification(request_id, choice, event)
            if result['outcome'] != 'ok':
                raise ValueError(result['error'])
            return self.state()

    def confirm_close(self, request_id, cancel=False):
        with self.lock:
            pending = self.pending_close
            if pending is None or pending['id'] != request_id:
                raise ValueError('That close request is no longer pending')
            self.pending_close = None
            event = {'timestamp': datetime.now(timezone.utc).isoformat(), 'source': 'manual_confirmation',
                     'request': ('Cancel' if cancel else 'Confirm') + ' closing ' + pending['label'],
                     'action': 'cancel_close' if cancel else pending['action'], 'target': pending['tab'],
                     'pane': pending['pane'], 'target_label': pending['label'], 'outcome': 'ok', 'error': None,
                     'close_request_id': request_id, 'before': self.observe()}
            try:
                if not cancel:
                    if pending['action'] == 'close_tab':
                        self.close_tab(pending['tab'], pending['identities'])
                    else:
                        self.close_pane(pending['tab'], pending['pane'], pending['identities'][pending['pane']])
            except Exception as error:
                event.update(outcome='error', error=str(error))
                raise
            finally:
                event['after'] = self.observe()
                self.record_event(event)
            return self.state()

    def select_tab(self, window_id, pane_id=None):
        before = self.observe()
        if window_id not in {tab["id"] for tab in before["tabs"]}:
            raise ValueError("That tab is no longer available")
        if pane_id is not None:
            self.validate_pane(window_id, pane_id)
        self.focus(window_id, pane_id)
        return self.state()

    def terminal_input(self, window_id, text=None, key=None, pane_id=None, identity=None):
        if not isinstance(window_id, str):
            raise ValueError("A tab id is required")
        if (text is None) == (key is None):
            raise ValueError("Send exactly one of text or key")
        if text is not None and (not isinstance(text, str) or not text or len(text) > 4096):
            raise ValueError("Terminal text must contain 1 to 4096 characters")
        if key is not None and key not in TERMINAL_KEYS:
            raise ValueError("Unsupported terminal key")
        with self.input_lock:
            before = self.observe()
            if window_id != before["selected"]:
                raise ValueError("That tab is no longer selected")
            if pane_id is None or identity is None:
                raise ValueError('An explicit pane id and identity are required')
            self.validate_pane(window_id, pane_id, identity)
            if pane_id != before['selected_pane']:
                raise ValueError('That pane is no longer selected')
            pane = pane_id
            if text is not None and any(character in text for character in "\r\n\t"):
                buffer = "jev-input-" + uuid.uuid4().hex
                tmux(self.socket, "load-buffer", "-b", buffer, "-", input=text)
                tmux(self.socket, "paste-buffer", "-b", buffer, "-d", "-p", "-t", pane)
            elif text is not None:
                tmux(self.socket, "send-keys", "-t", pane, "-l", text)
            else:
                tmux(self.socket, "send-keys", "-t", pane, key)
            return self.state()

    def close_pane(self, window, pane, identity):
        with self.input_lock:
            self.validate_pane(window, pane, identity)
            self.check_last_pane(window)
            tmux(self.socket, 'kill-pane', '-t', pane)
            return self.state()

    def check_last_pane(self, window, whole_tab=False):
        windows = tmux(self.socket, 'list-windows', '-t', f'={self.source_session}',
                       '-F', '#{window_id}').splitlines()
        if len(windows) == 1 and (whole_tab or len(self.panes(window)) == 1):
            raise ValueError('Keep one sandbox tab open; create another tab before closing this one')

    def close_tab(self, window, identities):
        with self.input_lock:
            state = self.observe()
            tab = next((tab for tab in state['tabs'] if tab['id'] == window), None)
            if tab is None or {pane['id']: pane['identity'] for pane in tab['panes']} != identities:
                raise ValueError('The tab panes changed; refresh and try again')
            self.check_last_pane(window, whole_tab=True)
            tmux(self.socket, 'kill-window', '-t', f'{self.source_session}:{window}')
            return self.state()

    def create_group(self, name):
        state = self.observe()
        self.groups.create(name, state["tabs"])
        return self.state()

    def rename_group(self, group_id, name):
        if not isinstance(group_id, str):
            raise ValueError("A tab group id is required")
        state = self.observe()
        self.groups.rename(group_id, name, state["tabs"])
        return self.state()

    def move_tab(self, window_id, group_id, before=None):
        if not isinstance(window_id, str) or not isinstance(group_id, str):
            raise ValueError("A tab id and tab group id are required")
        if before is not None and not isinstance(before, str):
            raise ValueError("The tab position must be a tab id or null")
        state = self.observe()
        self.groups.move(window_id, group_id, before, state["tabs"])
        return self.state()

    def rename_tab(self, window_id, name):
        name = clean_name(name, "tab")
        state = self.observe()
        if window_id not in {tab["id"] for tab in state["tabs"]}:
            raise ValueError("That tab is no longer available")
        tmux(self.socket, "rename-window", "-t", f"{self.source_session}:{window_id}", name)
        return self.state()

    def create_tab(self, name, group_id):
        name = clean_name(name, "tab")
        if not isinstance(group_id, str):
            raise ValueError("A tab group id is required")
        before = self.observe()
        if group_id not in {group["id"] for group in before["groups"]}:
            raise ValueError("That tab group is no longer available")
        cwd = tmux(self.socket, "display-message", "-p", "-t",
                   f"{self.source_session}:{before['selected']}",
                   "#{pane_current_path}")
        shell = os.environ.get("SHELL") or "/bin/sh"
        command = shlex.join(["env", "-u", "TYPESAFE_API_KEY", "-u", "ELEVENLABS_API_KEY",
                              shell, "-l"])
        window_id = tmux(self.socket, "new-window", "-d", "-P", "-F", "#{window_id}",
                         "-t", f"{self.source_session}:", "-n", name, "-c", cwd, command)
        current = self.observe()
        self.groups.move(window_id, group_id, None, current["tabs"])
        self.focus(window_id)
        return self.state()


def make_server(playground, port=0, public_origin=None):
    class Handler(BaseHTTPRequestHandler):
        def send(self, code, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def start_stream(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ndjson')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()

        def stream_event(self, type, **data):
            self.wfile.write((json.dumps({'type': type, **data}) + '\n').encode())
            self.wfile.flush()

        def local_request(self):
            host = f"127.0.0.1:{self.server.server_port}"
            origin = f"http://{host}"
            if public_origin and self.headers.get("Host") == public_origin.removeprefix("https://"):
                origin = public_origin
                if self.command == "POST" and not self.headers.get("Origin"):
                    return False
            elif self.headers.get("Host") != host:
                return False
            return self.headers.get("Origin", origin) == origin

        def do_GET(self):
            if not self.local_request():
                self.send(403, {"error": "Use this playground's loopback URL"})
            elif self.path == "/":
                self.send(200, Path(__file__).with_name("index.html").read_bytes(),
                          "text/html; charset=utf-8")
            elif self.path == "/api/state":
                try:
                    self.send(200, playground.state())
                except Exception as error:
                    self.send(503, {"error": str(error)})
            elif self.path in ("/voice.js", "/readback.js", "/microphone.js", "/terminal.js"):
                self.send(200, Path(__file__).with_name(self.path[1:]).read_bytes(), "text/javascript")
            elif self.path == "/favicon.ico":
                self.send(204, b"")
            else:
                self.send(404, {"error": "Not found"})

        def do_POST(self):
            if not self.local_request():
                self.send(403, {"error": "Cross-origin requests are not allowed"})
                return
            if self.path not in ("/api/request", "/api/select", "/api/input", "/api/speech", "/api/spoken-version", '/api/replies/heard',
                                 "/api/voice-event", "/api/groups/create", "/api/groups/rename",
                                 "/api/tabs/create", "/api/tabs/rename", "/api/tabs/move",
                                 "/api/tabs/close", "/api/panes/close",
                                 '/api/close/confirm', '/api/close/cancel', '/api/clarification'):
                self.send(404, {"error": "Not found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                limit = 128000 if self.path in ("/api/speech", "/api/request", "/api/spoken-version") else 8192
                if self.headers.get_content_type() != "application/json" or not 0 < size <= limit:
                    raise ValueError(f"Send a JSON request up to {limit} bytes")
                body = json.loads(self.rfile.read(size))
                if self.path == '/api/spoken-version':
                    if body.get('stream'):
                        self.start_stream()
                        try:
                            result = playground.readbacks.render(body['text'], body.get('mode', 'spoken'),
                                      lambda text: self.stream_event('text', text=text))
                            playground.voice.record('spoken_rendition', original=body['text'], **result)
                            self.stream_event('result', result=result)
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                        except Exception as error:
                            try:
                                self.stream_event('error', error=str(error))
                            except (BrokenPipeError, ConnectionResetError):
                                pass
                    else:
                        result = playground.readbacks.render(body['text'], body.get('mode', 'spoken'))
                        playground.voice.record('spoken_rendition', original=body['text'], **result)
                        self.send(200, result)
                    return
                if self.path == "/api/speech":
                    self.send(200, playground.voice.speak(body["text"], body.get('profile', 'current')), "audio/mpeg")
                    return
                if self.path == "/api/voice-event":
                    if body["kind"] not in ("mic_started", "mic_stopped", "playback_started", "playback_ended",
                                             "playback_cancelled", "voice_error", "speech_detected"):
                        raise ValueError("Unknown voice event")
                    playground.voice.record(body["kind"], detail=str(body.get("detail", ""))[:2000])
                    self.send(200, {"ok": True})
                    return
                if self.path == "/api/select":
                    window_id = body["tab"]
                    if not isinstance(window_id, str):
                        raise ValueError("A tab id is required")
                    self.send(200, playground.select_tab(window_id, body.get('pane')))
                    return
                if self.path == '/api/replies/heard':
                    playground.heard_reply(body)
                    self.send(200, {'ok': True})
                    return
                if self.path == "/api/input":
                    self.send(200, playground.terminal_input(
                        body["tab"], text=body.get("text"), key=body.get("key"),
                        pane_id=body['pane'], identity=body['identity']))
                    return
                if self.path == '/api/panes/close':
                    if not isinstance(body['identity'], str):
                        raise ValueError('A pane identity is required')
                    self.send(200, playground.close_pane(body['tab'], body['pane'], body['identity']))
                    return
                if self.path == '/api/tabs/close':
                    self.send(200, playground.close_tab(body['tab'], body['identities']))
                    return
                if self.path in ('/api/close/confirm', '/api/close/cancel'):
                    self.send(200, playground.confirm_close(body['id'], cancel=self.path.endswith('/cancel')))
                    return
                if self.path == '/api/clarification':
                    self.send(200, playground.resolve_clarification(body['id'], body['choice']))
                    return
                if self.path == "/api/groups/create":
                    self.send(200, playground.create_group(body["name"]))
                    return
                if self.path == "/api/groups/rename":
                    self.send(200, playground.rename_group(body["group"], body["name"]))
                    return
                if self.path == "/api/tabs/create":
                    self.send(200, playground.create_tab(body["name"], body["group"]))
                    return
                if self.path == "/api/tabs/rename":
                    self.send(200, playground.rename_tab(body["tab"], body["name"]))
                    return
                if self.path == "/api/tabs/move":
                    self.send(200, playground.move_tab(
                        body["tab"], body["group"], body.get("before")))
                    return
                request = body["request"]
                if not isinstance(request, str) or not request.strip():
                    raise ValueError("A nonempty request string is required")
            except (ValueError, KeyError, TypeError) as error:
                self.send(400, {"error": str(error)})
                return
            except (RuntimeError, OSError) as error:
                self.send(502, {"error": str(error)})
                return
            streaming = body.get('stream', False)
            if streaming:
                self.start_stream()
            event = playground.submit(request, "voice" if body.get("source") == "voice" else "typed",
                                      request_id=body.get("request_id"), capture_target=body.get('capture_target'),
                                      attention_target=body.get('attention_target'),
                                      on_text=(lambda text: self.stream_event('text', text=text)) if streaming else None)
            if streaming:
                try:
                    self.stream_event('result', result=event)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.send(200 if event["outcome"] == "ok" else 502, event)

        def log_message(self, *_):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-session", default="tabs")
    parser.add_argument("--view-session")
    parser.add_argument("--redact-terminal-logs", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--independent-selection", action="store_true")
    parser.add_argument("--public-origin")
    parser.add_argument("--voice-port", type=int, default=0)
    args = parser.parse_args()
    if args.public_origin and not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]+)?", args.public_origin):
        parser.error("public-origin must be an exact HTTPS origin")
    load_voice_settings()
    app = Playground(args.socket, args.run_dir / "events.jsonl",
                     source_session=args.source_session, view_session=args.view_session,
                     redact_terminal_logs=args.redact_terminal_logs,
                     independent_selection=args.independent_selection)
    server = make_server(app, args.port, args.public_origin)
    url = f"http://127.0.0.1:{server.server_port}"
    app.voice.start(url, port=args.voice_port, public_origin=args.public_origin)
    (args.run_dir / "url").write_text(url)
    print(url, flush=True)
    app.readback_preparation.start(app.readback_agents)
    try:
        server.serve_forever()
    finally:
        app.readback_preparation.stop()
        app.readbacks.pool.shutdown(wait=True, cancel_futures=True)
