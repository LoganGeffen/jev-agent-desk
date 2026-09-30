"""Run synthetic routing checks against Jev without touching terminals."""
import json

import jev


TABS = [
    {'id': '@a', 'name': 'Alpha', 'agent': True,
     'panes': [{'id': '%a', 'number': 1, 'active': True, 'agent': True}]},
    {'id': '@b', 'name': 'Beta', 'agent': True,
     'panes': [{'id': '%b', 'number': 1, 'active': True, 'agent': True}]},
]
CASES = [
    ('Read its latest reply.', 'read_reply', '@a'),
    ('What did Beta say?', 'read_reply', '@b'),
    ('Can you read that back to me?', 'read_reply', '@a'),
    ('Read the response from Beta again, please.', 'read_reply', '@b'),
    ('Explain that reply.', 'ask_session', '@a'),
    ('Summarize what it found.', 'ask_session', '@a'),
    ('What is Beta doing?', 'ask_session', '@b'),
    ('Did Beta finish the tests?', 'ask_session', '@b'),
    ('Can you tell me what Beta is doing?', 'ask_session', '@b'),
    ('Why does that reply say deployment is unverified?', 'ask_session', '@a'),
    ('Explain what Beta means in its last reply without asking it anything.', 'ask_session', '@b'),
    ('Why did you choose that?', 'send_message', '@a'),
    ('Can you fix it?', 'send_message', '@a'),
    ('Can you check whether the tests passed?', 'send_message', '@a'),
    ('Ask Beta whether the tests passed.', 'send_message', '@b'),
    ('Can you ask it what it is doing?', 'send_message', '@a'),
    ('Tell Beta: read that back to me.', 'send_message', '@b'),
    ('Beta, please explain why you changed that.', 'send_message', '@b'),
    ('Please inspect the Beta logs and fix the issue.', 'send_message', '@a'),
    ('Read the source files and fix the failing test.', 'send_message', '@a'),
    ('Switch to Beta.', 'select_tab', '@b'),
    ('Stop Beta.', 'interrupt_turn', '@b'),
    ('Actually never mind, do not send anything.', 'no_action', None),
    ('Stop reading.', 'no_action', None),
    ('Tell Beta: Stop Alpha and explain the deployment.', 'send_message', '@b'),
    ('Read the reply and then send Beta a request to fix it.', 'no_action', None),
    ('Okay. Please tell it that I want to extend this HTML. Compare both designs and implement the simpler one.', 'send_message', '@a'),
    ('Okay we need to fix these issues. ' + 'Beta had errors; explain and correct them. ' * 60, 'send_message', '@a'),
    ('What did Alpha say about Beta?', 'ask_session', '@a'),
    ('Read Alpha, actually Beta, back to me.', 'read_reply', '@b'),
    ('Tell me whether Beta has deployed anything yet.', 'ask_session', '@b'),
    ('Ask Beta to explain its last answer.', 'send_message', '@b'),
    ('Please summarize that response without sending anything.', 'ask_session', '@a'),
    ('Beta, read the code and tell me why it fails.', 'send_message', '@b'),
    ('Read the latest reply from Orion.', 'read_reply', 'none'),
]


def run():
    results = []
    for text, action, target in CASES:
        payload = jev.question(text, TABS, '@a')
        steps = []
        response = jev.interpret(payload, jev.evaluate, steps)
        actual = response['answers']['action']['choice']
        recipient = response['answers'].get('target', {}).get('choice')
        pane = response['answers'].get('pane:' + str(recipient), {}).get('choice')
        pane_ok = (actual not in ('send_message', 'read_reply', 'ask_session', 'interrupt_turn')
                   or recipient == 'none' or pane == recipient.replace('@', '%'))
        results.append({'request': text, 'expected': action, 'actual': actual,
                        'target': recipient, 'pane': pane,
                        'passed': actual == action and (target is None or target == recipient) and pane_ok,
                        'steps': steps})
    return results


if __name__ == '__main__':
    results = run()
    print(json.dumps(results, indent=2))
    raise SystemExit(0 if all(result['passed'] for result in results) else 1)
