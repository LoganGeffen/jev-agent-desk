"""Real subscription-backed Haiku tool round trip with one harmless synthetic tool.

The proof is a random code that exists only in the parent-process tool callback; the model can report it
in its final answer only by calling the restricted tool over the private MCP bridge.

Usage: python probe_jarvis_haiku.py [output.json]
"""
import json
import secrets
import subprocess
import sys
import time
from unittest.mock import patch

import jarvis_haiku


TOOL = 'fetch_probe_code'
TOOLS = [{'name': TOOL, 'description': 'Fetch the probe code for a slot. The code cannot be known without this tool.',
          'inputSchema': {'type': 'object', 'properties': {'slot': {'type': 'string', 'enum': ['alpha', 'beta']}},
                          'required': ['slot'], 'additionalProperties': False}}]
USER_TEXT = 'Fetch the probe code for slot alpha and tell me the exact code.'


def main():
    nonce = f'probe-{secrets.token_hex(8)}'
    context = {'session_id': 'probe', 'revision': 1, 'subject': None, 'user_input_authority': 'current_turn.user_text',
               'current_turn': {'turn_id': 'probe-turn', 'user_text': USER_TEXT, 'status': 'running'},
               'recent_turns': []}
    executed = []

    def execute_tool(name, arguments):
        executed.append({'name': name, 'arguments': arguments})
        return {'ok': True, 'status': 'completed', 'output': {'slot': arguments['slot'], 'code': nonce}}

    launches = []
    real_popen = subprocess.Popen

    def launch(command, **kwargs):
        launches.append(command)
        return real_popen(command, **kwargs)

    chunks = []
    report = {'probe': 'jarvis-haiku-nonce-tool-roundtrip', 'requested_model': jarvis_haiku.MODEL}
    started = time.monotonic()
    try:
        with patch.object(jarvis_haiku.subprocess, 'Popen', side_effect=launch):
            result = jarvis_haiku.run_turn(context, TOOLS, execute_tool, chunks.append)
        report.update(outcome='success', **{key: value for key, value in result.items() if key != 'tool_calls'})
        calls = result['tool_calls']
    except jarvis_haiku.TurnError as error:
        report.update(outcome='failed', error=str(error), text=error.text)
        calls = error.tool_calls
    report['wall_ms'] = round((time.monotonic() - started) * 1000)
    report['streamed_chunks'] = len(chunks)
    report['callback_count'] = len(executed)
    report['parent_executions'] = executed
    report['tool_call_records'] = [{key: call.get(key) for key in ('tool', 'arguments', 'duration_ms')} |
                                   {'ok': 'result' in call} for call in calls]
    command = launches[0] if launches else []
    report['flags'] = [flag for flag in command if flag != jarvis_haiku.INSTRUCTIONS]
    prompt_surface = json.dumps([context, command, jarvis_haiku.INSTRUCTIONS])
    report['checks'] = {
        'succeeded': report['outcome'] == 'success',
        'nonce_only_from_tool': nonce not in prompt_surface,
        'answer_has_nonce': nonce in report.get('text', ''),
        'exactly_one_callback': executed == [{'name': TOOL, 'arguments': {'slot': 'alpha'}}],
        'init_tools_restricted': report.get('init_tools') == [f'mcp__jarvis__{TOOL}'],
        'actual_model_is_requested': report.get('model') == jarvis_haiku.MODEL,
    }
    report['passed'] = all(report['checks'].values())
    output = json.dumps(report, indent=2)
    if len(sys.argv) > 1:
        with open(sys.argv[1], 'w') as file:
            file.write(output + '\n')
    print(output)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
