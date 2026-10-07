import hashlib
import threading
import time

import claude_actions
import codex_actions


def history(agent):
    if agent.get('provider') == 'claude':
        binding = claude_actions.check_target(agent['socket'], agent)
        result = claude_actions.thread_read(binding)
        claude_actions.check_target(agent['socket'], agent)
    else:
        codex_actions.check_target(agent['socket'], agent)
        result = codex_actions.thread_read(agent['thread_id'])
        codex_actions.check_target(agent['socket'], agent)
    return result


def matching_reply(thread, baseline, text):
    matches = []
    for turn in thread['turns']:
        if turn['id'] in baseline:
            continue
        messages = ['\n'.join(part['text'] for part in item['content'] if part['type'] == 'text')
                    for item in turn['items'] if item['type'] == 'userMessage']
        if text in messages:
            matches.append(turn)
    if len(matches) != 1:
        return None
    turn = matches[0]
    if turn['status'] != 'completed':
        return None
    replies = [item['text'] for item in turn['items']
               if item['type'] == 'agentMessage' and item.get('phase') in (None, 'final_answer')]
    return {'turn_id': turn['id'], 'text': replies[-1]} if replies else None


class ReplyWatch:
    def __init__(self):
        self.notices = []
        self.lock = threading.Lock()

    def view(self):
        with self.lock:
            return list(self.notices)

    def start(self, agent, baseline, text, label, request_id):
        def observe():
            deadline = time.monotonic() + 1800
            while time.monotonic() < deadline:
                try:
                    reply = matching_reply(history(agent), baseline, text)
                except (RuntimeError, OSError, ValueError) as error:
                    notice = {'error': f'Reply observation stopped: {error}'}
                    break
                if reply:
                    notice = reply
                    break
                time.sleep(3)
            else:
                notice = {'error': 'Reply observation expired without a confirmed completed reply.'}
            identity = f"{agent['process']}:{agent.get('provider', 'codex')}:{agent['thread_id']}"
            key = '\0'.join(str(part) for part in (request_id, agent['window_id'], agent['pane_id'], identity,
                                                  notice.get('turn_id') or notice.get('error')))
            notice_id = hashlib.sha256(key.encode()).hexdigest()[:32]
            with self.lock:
                if all(item['id'] != notice_id for item in self.notices):
                    self.notices.append({'id': notice_id, 'request_id': request_id,
                                         'tab': agent['window_id'], 'pane': agent['pane_id'],
                                         'identity': identity, 'thread_id': agent['thread_id'], 'target': label, **notice})
                    self.notices = self.notices[-50:]
        threading.Thread(target=observe, daemon=True).start()


class ReadbackPreparation:
    def __init__(self, cache, attention=None, on_reply=None, activity=None):
        self.cache = cache
        self.attention = attention
        self.on_reply = on_reply
        self.activity = activity
        self.observed = {}
        self.latest = {}
        self.stopped = threading.Event()
        self.worker = None

    def poll(self, agents):
        if self.attention:
            self.attention.retain(agents)
        current = {}
        observed_agents = {}
        changed = []
        for agent in agents:
            if self.stopped.is_set():
                return
            key = (agent['provider'], agent['thread_id'], agent['process'])
            if key in self.latest:
                current[key] = self.latest[key]
            try:
                thread = history(agent)
            except (RuntimeError, OSError, ValueError):
                self.observed.pop(key, None)
                if self.attention:
                    self.attention.unavailable(agent)
                continue
            activity = {'state': 'unknown', 'turn_id': None}
            if self.activity:
                try:
                    activity = self.activity(agent, thread)
                    provider = claude_actions if agent['provider'] == 'claude' else codex_actions
                    provider.check_target(agent['socket'], agent)
                except (RuntimeError, OSError, ValueError):
                    activity = {'state': 'unknown', 'turn_id': None}
                thread = {**thread, 'activity': activity}
            if self.attention:
                self.attention.observe(agent, thread)
            observed = None
            for turn in reversed(thread['turns']):
                if turn['status'] != 'completed':
                    continue
                replies = [item['text'] for item in turn['items']
                           if item['type'] == 'agentMessage' and item.get('phase') in (None, 'final_answer')]
                if not replies:
                    continue
                text = replies[-1]
                if text.strip():
                    observed = (turn['id'], text)
                if text.strip() and len(text) <= 20000:
                    current[key] = (turn['id'], text)
                    if current[key] != self.latest.get(key):
                        changed.append(text)
                else:
                    current.pop(key, None)
                break
            if self.on_reply:
                try:
                    latest = thread['turns'][-1] if thread['turns'] else None
                    eligible = (observed and latest and latest['id'] == observed[0]
                                and activity['state'] == 'ready'
                                and activity.get('turn_id') == observed[0])
                    known = activity['state'] != 'unknown'
                    self.on_reply(agent, observed[0] if eligible else None,
                                  notify=known and key in self.observed
                                  and self.observed[key] == activity.get('generation'))
                    if known:
                        observed_agents[key] = activity.get('generation')
                except Exception:
                    pass
        self.cache.retain(value[1] for value in current.values())
        for text in dict.fromkeys(changed):
            self.cache.prepare(text)
        self.latest = current
        self.observed = observed_agents

    def start(self, get_agents):
        def observe():
            while not self.stopped.is_set():
                try:
                    self.poll(get_agents())
                except (RuntimeError, OSError, ValueError):
                    pass
                self.stopped.wait(3)
        self.worker = threading.Thread(target=observe, daemon=True, name='readback-preparation')
        self.worker.start()

    def stop(self):
        self.stopped.set()
        if self.worker:
            self.worker.join(timeout=5)
