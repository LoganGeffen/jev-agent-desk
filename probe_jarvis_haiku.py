"""Real subscription-backed Haiku tool round trip with one harmless synthetic tool.

Usage: python probe_jarvis_haiku.py [output.json]
"""
import json
import subprocess
import sys
import time
from unittest.mock import patch

import jarvis_haiku


TOOLS = [{'name': 'lookup_widget', 'description': 'Look up a widget by id and return its color.',
          'input_schema': {'type': 'object', 'properties': {'widget_id': {'type': 'string'}},
                           'required': ['widget_id']}}]
CONTEXT = {'instructions': 'Answer in one short sentence after using the tool.',
           'input': {'question': 'What color is widget w-17?'}}


def main():
    executed = []

    def execute_tool(name, arguments):
        executed.append({'name': name, 'arguments': arguments})
        return {'widget_id': arguments['widget_id'], 'color': 'teal'}

    launches = []
    real_popen = subprocess.Popen

    def launch(command, **kwargs):
        launches.append(command)
        return real_popen(command, **kwargs)

    chunks = []
    report = {'probe': 'jarvis-haiku-tool-roundtrip', 'requested_model': jarvis_haiku.MODEL}
    started = time.monotonic()
    try:
        with patch.object(jarvis_haiku.subprocess, 'Popen', side_effect=launch):
            result = jarvis_haiku.run_turn(CONTEXT, TOOLS, execute_tool, chunks.append)
        report.update(outcome='success', **result)
    except jarvis_haiku.TurnError as error:
        report.update(outcome='failed', error=str(error), text=error.text, tool_calls=error.tool_calls)
    report['wall_ms'] = round((time.monotonic() - started) * 1000)
    report['streamed_chunks'] = len(chunks)
    report['parent_executions'] = executed
    flags = launches[0] if launches else []
    report['flags'] = [flag for flag in flags if flag != jarvis_haiku.PREAMBLE + CONTEXT['instructions']]
    report['passed'] = (report['outcome'] == 'success' and 'teal' in report.get('text', '').lower()
                        and executed == [{'name': 'lookup_widget', 'arguments': {'widget_id': 'w-17'}}])
    output = json.dumps(report, indent=2)
    if len(sys.argv) > 1:
        with open(sys.argv[1], 'w') as file:
            file.write(output + '\n')
    print(output)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
