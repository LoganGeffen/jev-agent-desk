import copy
from contextlib import nullcontext
import json
import threading
import time

from jarvis_state import JarvisState


TARGET_SCHEMA = {'type': 'object', 'properties': {key: {'type': 'string'} for key in
                 ('tab', 'pane', 'identity', 'provider', 'thread_id', 'process', 'label')},
                 'required': ['tab', 'pane', 'identity'], 'additionalProperties': False}
CORE_TOOLS = [
    {'name': 'set_subject', 'description': 'Remember a resolved conversation independently of visible selection.',
     'inputSchema': {'type': 'object', 'properties': {'target': TARGET_SCHEMA}, 'required': ['target'], 'additionalProperties': False}},
    {'name': 'prepare_literal', 'description': 'Capture an exact span from the current user input as a draft; never send.',
     'inputSchema': {'type': 'object', 'properties': {'start': {'type': 'integer'}, 'end': {'type': 'integer'},
                     'supersedes': {'type': 'string'}}, 'required': ['start', 'end'], 'additionalProperties': False}},
    {'name': 'send_draft', 'description': 'Submit an application-owned exact draft to its captured recipient only after authorization.',
     'inputSchema': {'type': 'object', 'properties': {'draft_id': {'type': 'string'}}, 'required': ['draft_id'], 'additionalProperties': False}},
]


def validate_arguments(value, schema):
    kinds = {'object': dict, 'array': list, 'string': str, 'integer': int, 'number': (int, float), 'boolean': bool}
    kind = schema.get('type')
    if kind in kinds and (not isinstance(value, kinds[kind]) or kind in ('integer', 'number') and isinstance(value, bool)):
        raise ValueError('Tool argument has an invalid type')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('Tool argument is outside the allowed values')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        if any(key not in value for key in schema.get('required', [])):
            raise ValueError('Tool argument is missing a required field')
        if schema.get('additionalProperties') is False and any(key not in properties for key in value):
            raise ValueError('Tool argument contains an unknown field')
        for key, item in value.items():
            if key in properties:
                validate_arguments(item, properties[key])
    if isinstance(value, list) and 'items' in schema:
        for item in value:
            validate_arguments(item, schema['items'])
    json.dumps(value, allow_nan=False)


class JarvisController:
    def __init__(self, provider, execute_tool, tool_specs=(), state=None, authorize=None, effect_tools=()):
        self.provider = provider
        self.execute_tool = execute_tool
        self.state = state or JarvisState()
        self.authorize = authorize or (lambda name, arguments, turn: False)
        self.effect_tools = set(effect_tools) | {'send_draft', 'interrupt_agent', 'close_session'}
        self.tool_specs = copy.deepcopy(CORE_TOOLS + list(tool_specs))
        names = [tool['name'] for tool in self.tool_specs]
        if len(set(names)) != len(names):
            raise ValueError('Duplicate controller tool name')
        self.specs = {tool['name']: tool for tool in self.tool_specs}
        self.effect_lock = threading.Lock()
        self.control_lock = threading.RLock()
        self.cancels = {}

    def snapshot(self):
        return self.state.snapshot()

    def cancel(self, turn_id=None):
        with self.control_lock:
            turn = self.state.cancel(turn_id)
            if turn and turn['turn_id'] in self.cancels:
                self.cancels[turn['turn_id']].set()
            return turn

    def receipt(self, turn):
        snapshot = self.state.snapshot()
        return {'session_id': self.state.session_id, 'turn_id': turn['turn_id'],
                'request_id': turn['request_id'], 'revision': snapshot['revision'],
                'status': turn['status'], 'text': turn.get('result', {}).get('text', ''),
                'actions': [action for action in snapshot['actions'].values() if action['turn_id'] == turn['turn_id']],
                **({'error': turn['error']} if 'error' in turn else {})}

    def submit(self, text, request_id, capture_target=None, on_text=None, source='typed', mode='jarvis'):
        with self.control_lock:
            previous = self.state.active_turn_id
            turn, fresh = self.state.begin_turn(text, request_id, source, mode, capture_target)
            if not fresh:
                return self.receipt(turn)
            if previous in self.cancels:
                self.cancels[previous].set()
            cancel = self.cancels[turn['turn_id']] = threading.Event()
        deadline = time.monotonic() + 60
        calls = 0

        def guard():
            with self.state.lock:
                self.state.guard(turn['turn_id'])
            if cancel.is_set():
                raise InterruptedError('Controller turn cancelled')
            if time.monotonic() > deadline:
                raise TimeoutError('Controller inference exceeded 60 seconds')

        def dispatch(name, arguments):
            nonlocal calls
            guard()
            if name not in self.specs:
                raise ValueError('Unknown controller tool')
            validate_arguments(arguments, self.specs[name]['inputSchema'])
            calls += 1
            if calls > 8:
                raise ValueError('Controller tool call limit exceeded')
            with self.effect_lock if name in self.effect_tools else nullcontext():
                guard()
                if name == 'prepare_literal':
                    draft = self.state.prepare_literal(turn['turn_id'], **arguments)
                    return {'ok': True, 'status': 'prepared', 'draft_id': draft['draft_id'], 'target': draft['target']}
                app_turn = {key: copy.deepcopy(turn[key]) for key in ('turn_id', 'request_id', 'user_text', 'capture_target')}
                app_turn.update(session_id=self.state.session_id, subject=self.state.snapshot()['subject'])
                action = None
                handler_arguments = copy.deepcopy(arguments)
                if name in self.effect_tools:
                    if not self.authorize(name, copy.deepcopy(arguments), app_turn):
                        return {'ok': False, 'status': 'not_attempted', 'error': 'The user has not authorized this action'}
                    guard()
                    draft_id = arguments.get('draft_id') if name == 'send_draft' else None
                    action = self.state.begin_action(turn['turn_id'], name, arguments, draft_id)
                    if draft_id:
                        handler_arguments = {'draft_id': draft_id, 'target': action['target'], 'text': action['payload']}
                    handler_arguments['action_id'] = action['action_id']
                try:
                    result = self.execute_tool(name, handler_arguments, app_turn)
                    if not isinstance(result, dict) or type(result.get('ok')) is not bool or not isinstance(result.get('status'), str):
                        raise ValueError('Application tool returned an invalid receipt')
                    json.dumps(result, allow_nan=False)
                    if action:
                        self.state.finish_action(action['action_id'], result)
                        result = dict(result, action_id=action['action_id'])
                    if result.get('job_id'):
                        self.state.record_job(turn['turn_id'], result, subject=app_turn['subject'])
                    guard()
                    if result.get('ok') and result.get('subject') and not result.get('job_id'):
                        self.state.set_subject(turn['turn_id'], result['subject'])
                    if 'output' in result:
                        result = dict(result, **self.state.record_evidence(turn['turn_id'], name, result['output']))
                    guard()
                    return copy.deepcopy(result)
                except BaseException as error:
                    if action and self.state.snapshot()['actions'][action['action_id']]['status'] == 'attempting':
                        self.state.finish_action(action['action_id'], {'ok': False, 'status': 'uncertain', 'error': str(error)})
                    raise

        def stream(delta):
            with self.control_lock:
                guard()
                if not isinstance(delta, str):
                    raise ValueError('Controller text delta must be text')
                if on_text:
                    on_text(delta)

        try:
            result = self.provider(self.state.context(turn['turn_id']), self.tool_specs, dispatch,
                                   on_text=stream, cancel=cancel)
            guard()
            if not isinstance(result, dict) or not isinstance(result.get('text'), str):
                raise ValueError('Controller provider returned no final text')
            return self.receipt(self.state.finish_turn(turn['turn_id'], result=result))
        except InterruptedError:
            self.state.cancel(turn['turn_id'])
            return self.receipt(self.state.snapshot()['turns'][turn['turn_id']])
        except Exception as error:
            return self.receipt(self.state.finish_turn(turn['turn_id'], error=error))
        finally:
            with self.control_lock:
                self.cancels.pop(turn['turn_id'], None)
