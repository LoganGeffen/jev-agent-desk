import threading
import time
import uuid

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
            with self.lock:
                self.notices.append({'id': uuid.uuid4().hex, 'request_id': request_id,
                                     'thread_id': agent['thread_id'], 'target': label, **notice})
                self.notices = self.notices[-50:]
        threading.Thread(target=observe, daemon=True).start()
