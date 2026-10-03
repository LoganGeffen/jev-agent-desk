import hmac
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SERVER = 'jarvis'
MAX_BODY = 1 << 20
MAX_CALLS = 8
NAME = re.compile(r'[A-Za-z0-9_]{1,40}\Z')
KEYWORDS = {'type', 'description', 'enum', 'properties', 'required', 'items', 'maxLength'}
TYPES = {'string': str, 'boolean': bool, 'object': dict, 'array': list, 'integer': int, 'number': (int, float)}


def check_schema(schema, path='input_schema'):
    unsupported = set(schema) - KEYWORDS
    if unsupported or schema.get('type') not in TYPES:
        raise ValueError(f'{path} uses unsupported schema: {sorted(unsupported) or schema.get("type")}')
    for name, child in schema.get('properties', {}).items():
        check_schema(child, f'{path}.{name}')
    if 'items' in schema:
        check_schema(schema['items'], f'{path}[]')


def validate(schema, value, path='arguments'):
    kind = schema['type']
    if not isinstance(value, TYPES[kind]) or (kind in ('integer', 'number') and isinstance(value, bool)):
        raise ValueError(f'{path} must be {kind}')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path} must be one of {schema["enum"]}')
    if kind == 'string' and len(value) > schema.get('maxLength', len(value)):
        raise ValueError(f'{path} is longer than {schema["maxLength"]}')
    if kind == 'array':
        for index, item in enumerate(value):
            validate(schema['items'], item, f'{path}[{index}]')
    if kind == 'object':
        properties = schema.get('properties', {})
        extra = set(value) - set(properties)
        if extra:
            raise ValueError(f'{path} has unexpected properties: {sorted(extra)}')
        missing = set(schema.get('required', ())) - set(value)
        if missing:
            raise ValueError(f'{path} is missing: {sorted(missing)}')
        for name, item in value.items():
            validate(properties[name], item, f'{path}.{name}')


class ToolBridge:
    """Loopback MCP server exposing only app-provided tools; execute_tool runs in the parent."""

    def __init__(self, tool_specs, execute_tool, cancel=None, max_calls=MAX_CALLS):
        self.schemas = {}
        self.listing = []
        for spec in tool_specs:
            name = spec['name']
            if not NAME.match(name) or name in self.schemas or spec['input_schema'].get('type') != 'object':
                raise ValueError(f'Invalid or duplicate tool {name!r}')
            check_schema(spec['input_schema'], name)
            self.schemas[name] = spec['input_schema']
            self.listing.append({'name': name, 'description': spec['description'],
                                 'inputSchema': spec['input_schema']})
        if not self.schemas:
            raise ValueError('At least one tool is required')
        self.tool_names = [f'mcp__{SERVER}__{name}' for name in self.schemas]
        self.calls = []
        self._execute = execute_tool
        self._cancel = cancel
        self._max_calls = max_calls
        self._lock = threading.Lock()
        self._closed = False
        self._token = secrets.token_urlsafe(32)
        self._server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)

    def config(self):
        return {'mcpServers': {SERVER: {
            'type': 'http', 'url': f'http://127.0.0.1:{self._server.server_port}/mcp',
            'headers': {'Authorization': f'Bearer {self._token}'}}}}

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        with self._lock:
            self._closed = True
        self._server.shutdown()
        self._server.server_close()

    def call_tool(self, name, arguments):
        with self._lock:
            record = {'tool': name, 'arguments': arguments}
            self.calls.append(record)
            if self._closed or (self._cancel and self._cancel.is_set()):
                record['error'] = 'cancelled before dispatch'
            elif len(self.calls) > self._max_calls:
                record['error'] = 'tool call limit reached'
            else:
                try:
                    if not isinstance(name, str) or name not in self.schemas:
                        raise ValueError(f'unknown tool {name!r}')
                    validate(self.schemas[name], arguments)
                except ValueError as error:
                    record['error'] = f'invalid call: {error}'
                else:
                    try:
                        record['result'] = self._execute(name, arguments)
                        json.dumps(record['result'])
                    except Exception as error:
                        record['error'] = f'{type(error).__name__}: {error}'
            if 'error' in record:
                return f'Tool did not complete ({record["error"]}). Do not claim it happened.', True
            return json.dumps(record['result']), False

    def _handler(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=None):
                data = json.dumps(body).encode() if body is not None else b''
                self.send_response(status)
                if body is not None:
                    self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply(405)

            def do_POST(self):
                expected = f'Bearer {bridge._token}'
                if not hmac.compare_digest(self.headers.get('Authorization', ''), expected):
                    return self.reply(401)
                try:
                    length = int(self.headers.get('Content-Length', ''))
                    if self.path != '/mcp' or not 0 < length <= MAX_BODY:
                        return self.reply(404)
                    message = json.loads(self.rfile.read(length))
                    if not isinstance(message, dict):
                        raise ValueError
                except ValueError:
                    return self.reply(400)
                if 'id' not in message:
                    return self.reply(202)
                params = message.get('params') or {}
                method = message.get('method')
                if method == 'initialize':
                    result = {'protocolVersion': params.get('protocolVersion'), 'capabilities': {'tools': {}},
                              'serverInfo': {'name': SERVER, 'version': '1'}}
                elif method == 'ping':
                    result = {}
                elif method == 'tools/list':
                    result = {'tools': bridge.listing}
                elif method == 'tools/call':
                    text, failed = bridge.call_tool(params.get('name'), params.get('arguments', {}))
                    result = {'content': [{'type': 'text', 'text': text}], 'isError': failed}
                else:
                    return self.reply(200, {'jsonrpc': '2.0', 'id': message['id'],
                                            'error': {'code': -32601, 'message': 'Method not found'}})
                self.reply(200, {'jsonrpc': '2.0', 'id': message['id'], 'result': result})

        return Handler
