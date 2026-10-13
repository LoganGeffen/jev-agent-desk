import json
import os
import select
import subprocess
import tempfile
import time


TIMEOUT = 60


def stream_answer(question, evidence, instructions, model, on_text=None):
    started = time.monotonic()
    deadline = started + TIMEOUT
    text = ''
    first_text_ms = None
    actual_model = model
    environment = {key: value for key, value in os.environ.items() if key not in (
        'TYPESAFE_API_KEY', 'ELEVENLABS_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
        'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY')}
    environment['MAX_THINKING_TOKENS'] = '0'
    environment['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] = '1'
    command = ['claude', '-p', '--model', model, '--effort', 'low',
               '--output-format', 'stream-json', '--include-partial-messages', '--verbose',
               '--no-session-persistence', '--tools', '', '--strict-mcp-config', '--mcp-config',
               '{"mcpServers":{}}', '--setting-sources', '', '--settings',
               '{"disableAllHooks":true,"alwaysThinkingEnabled":false}', '--disable-slash-commands',
               '--no-chrome', '--system-prompt',
               'You transform supplied text or answer from supplied evidence. Never use tools.\n' + instructions]
    with tempfile.TemporaryDirectory(prefix='jev-haiku-answer-') as directory, \
         tempfile.TemporaryFile() as request, tempfile.TemporaryFile() as errors:
        request.write(json.dumps({'question': question, 'evidence': evidence}).encode())
        request.seek(0)
        process = subprocess.Popen(command, cwd=directory, stdin=request, stdout=subprocess.PIPE,
                                   stderr=errors, env=environment)
        pending = b''
        try:
            while True:
                while b'\n' not in pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                        raise TimeoutError('Spoken answer timed out')
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError('Claude spoken answer connection closed before completion')
                    pending += chunk
                line, pending = pending.split(b'\n', 1)
                event = json.loads(line)
                if event.get('type') == 'system' and event.get('subtype') == 'init':
                    actual_model = event.get('model', model)
                    if event.get('tools'):
                        raise RuntimeError('Claude spoken answer unexpectedly enabled tools')
                elif event.get('type') == 'stream_event':
                    stream = event.get('event', {})
                    delta = stream.get('delta', {})
                    if delta.get('type') == 'thinking_delta':
                        raise RuntimeError('Claude spoken answer unexpectedly enabled thinking')
                    if stream.get('content_block', {}).get('type') == 'tool_use':
                        raise RuntimeError('Claude spoken answer attempted a tool call')
                    if delta.get('stop_reason') == 'max_tokens':
                        raise RuntimeError('Claude spoken answer was truncated')
                    if delta.get('type') == 'text_delta' and delta.get('text'):
                        if first_text_ms is None:
                            first_text_ms = round((time.monotonic() - started) * 1000)
                        text += delta['text']
                        if on_text:
                            on_text(delta['text'])
                elif event.get('type') == 'result':
                    if event.get('is_error') or event.get('subtype') != 'success':
                        raise RuntimeError('Claude spoken answer did not complete')
                    if not text.strip():
                        raise RuntimeError('Spoken answer returned no text')
                    return {'text': text, 'model': actual_model, 'first_text_ms': first_text_ms,
                            'answer_ms': round((time.monotonic() - started) * 1000)}
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
