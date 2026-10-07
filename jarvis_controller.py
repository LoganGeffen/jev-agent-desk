import copy
from contextlib import nullcontext
import json
import threading
import time

from jarvis_state import DuplicateAction, JarvisState


TARGET_SCHEMA = {'type': 'object', 'properties': {key: {'type': 'string'} for key in
                 ('tab', 'pane', 'identity', 'provider', 'thread_id', 'process', 'label')},
                 'required': ['tab', 'pane', 'identity'], 'additionalProperties': False}
CORE_TOOLS = [
    {'name': 'read_user_source', 'description': 'Read a bounded exact page of an archived immutable user source. Never prepares or sends a message.',
     'inputSchema': {'type': 'object', 'properties': {'source_turn_id': {'type': 'string'}, 'offset': {'type': 'integer'}},
                     'required': ['source_turn_id'], 'additionalProperties': False}},
    {'name': 'set_subject', 'description': 'Resolve the subject for this turn from a validated conversation.',
     'inputSchema': {'type': 'object', 'properties': {'target': TARGET_SCHEMA}, 'required': ['target'], 'additionalProperties': False}},
    {'name': 'prepare_literal', 'description': 'Capture an exact span from the current user input as a draft; never send.',
     'inputSchema': {'type': 'object', 'properties': {'start': {'type': 'integer'}, 'end': {'type': 'integer'},
                     }, 'required': ['start', 'end'], 'additionalProperties': False}},
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
    def __init__(self, provider, execute_tool, tool_specs=(), state=None, authorize=None, effect_tools=(), literal_boundaries=None, response_policy=None, turn_guard=None, continuity_classifier=None, trace=None):
        self._trace = trace
        self.provider = provider
        self.execute_tool = execute_tool
        self.state = state or JarvisState()
        self.continuity_classifier = continuity_classifier
        self.turn_guard = turn_guard
        self.response_policy = response_policy
        self.literal_boundaries = literal_boundaries
        self.authorize = authorize or (lambda name, arguments, turn: False)
        self.effect_tools = set(effect_tools) | {'send_draft', 'interrupt_agent', 'close_session'}
        self.tool_specs = copy.deepcopy(CORE_TOOLS + list(tool_specs))
        if literal_boundaries:
            prepare = next(tool for tool in self.tool_specs if tool['name'] == 'prepare_literal')
            prepare['description'] = 'Prepare the intended exact user-message body using application-owned source boundaries; never send. Omit boundary_id to resolve the source. Select a returned boundary_id only if multiple candidates need disambiguation.'
            prepare['inputSchema'] = {'type': 'object', 'properties': {'boundary_id': {'type': 'string'}},
                                      'additionalProperties': False}
        names = [tool['name'] for tool in self.tool_specs]
        if len(set(names)) != len(names):
            raise ValueError('Duplicate controller tool name')
        self.specs = {tool['name']: tool for tool in self.tool_specs}
        self.effect_lock = threading.Lock()
        self.control_lock = threading.RLock()
        self.cancels = {}

    def snapshot(self):
        return self.state.snapshot()

    def trace(self, stage, **fields):
        try:
            if self._trace:
                self._trace(stage, **fields)
        except Exception:
            pass

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
                'subject': copy.deepcopy(turn.get('resolved_subject', turn['subject'])),
                'actions': [action for action in snapshot['actions'].values() if action['turn_id'] == turn['turn_id']],
                **({key: copy.deepcopy(turn['result'][key]) for key in ('readback', 'available_readback', 'raw_model_text', 'view_target') if key in turn.get('result', {})}),
                **({'error': turn['error']} if 'error' in turn else {})}

    def submit(self, text, request_id, capture_target=None, on_text=None, source='typed', mode='jarvis', subject=...):
        with self.control_lock:
            previous = self.state.active_turn_id
            turn, fresh = self.state.begin_turn(text, request_id, source, mode, capture_target, subject=subject)
            if not fresh:
                return self.receipt(turn)
            if previous in self.cancels:
                self.cancels[previous].set()
            cancel = self.cancels[turn['turn_id']] = threading.Event()
        deadline = time.monotonic() + 60
        calls = 0
        view_target = None

        def guard():
            if self.turn_guard:
                self.turn_guard(turn['request_id'])
            with self.state.lock:
                self.state.guard(turn['turn_id'])
            if cancel.is_set():
                raise InterruptedError('Controller turn cancelled')
            if time.monotonic() > deadline:
                raise TimeoutError('Controller inference exceeded 60 seconds')

        def perform(name, arguments):
            nonlocal calls, view_target
            guard()
            if name not in self.specs:
                raise ValueError('Unknown controller tool')
            validate_arguments(arguments, self.specs[name]['inputSchema'])
            calls += 1
            if calls > 8:
                raise ValueError('Controller tool call limit exceeded')
            with self.effect_lock if name in self.effect_tools else nullcontext():
                guard()
                app_turn = {key: copy.deepcopy(turn[key]) for key in ('turn_id', 'request_id', 'user_text', 'capture_target')}
                current = self.state.snapshot()['turns'][turn['turn_id']]
                app_turn.update(session_id=self.state.session_id, subject=current.get('resolved_subject', current['subject']))
                if name == 'read_user_source':
                    return self.state.read_user_source(**arguments)
                if name == 'prepare_literal':
                    span = arguments
                    if self.literal_boundaries:
                        options = self.state.snapshot()['turns'][turn['turn_id']].get('literal_boundaries')
                        identifier = arguments.get('boundary_id')
                        if not identifier and options is None:
                            options = self.state.set_literal_boundaries(turn['turn_id'], self.literal_boundaries(turn['user_text'], app_turn))
                        if identifier:
                            options = [option for option in options or [] if option['boundary_id'] == identifier]
                        if not options:
                            return {'ok': False, 'status': 'blocked', 'error': 'No authorized current-turn literal boundary'}
                        if len(options) != 1:
                            return {'ok': False, 'status': 'clarification', 'candidates': options}
                        span = {key: options[0][key] for key in ('start', 'end')}
                    draft = self.state.prepare_literal(turn['turn_id'], **span)
                    if self.literal_boundaries and options[0].get('form') in ('indirect_question', 'composition'):
                        draft = self.state.formulate_draft(turn['turn_id'], draft['draft_id'],
                                                          options[0]['formulated_text'], options[0]['form'])
                    return {'ok': True, 'status': 'prepared', 'draft_id': draft['draft_id'], 'target': draft['target'],
                            **{key: copy.deepcopy(draft[key]) for key in ('text', 'source_turn_id', 'source_span', 'form')}}
                action = None
                handler_arguments = copy.deepcopy(arguments)
                if name in self.effect_tools:
                    authorization_arguments = copy.deepcopy(arguments)
                    draft_id = arguments.get('draft_id') if name == 'send_draft' else None
                    if draft_id:
                        with self.state.lock:
                            self.state.guard(turn['turn_id'])
                            draft = self.state.drafts.get(draft_id)
                            if draft is None:
                                return {'ok': False, 'status': 'not_attempted', 'error': 'Unknown application draft'}
                            if draft['turn_id'] != turn['turn_id'] or draft['status'] != 'prepared':
                                raise ValueError('Draft lacks current-turn prepared provenance')
                            authorization_arguments.update(target=copy.deepcopy(draft['target']),
                                                           text=draft['text'], source_turn_id=draft['source_turn_id'])
                    if not self.authorize(name, authorization_arguments, copy.deepcopy(app_turn)):
                        return {'ok': False, 'status': 'not_attempted', 'error': 'The user has not authorized this action'}
                    guard()
                    try:
                        action = self.state.begin_action(turn['turn_id'], name, arguments, draft_id)
                    except DuplicateAction as duplicate:
                        previous = duplicate.action
                        result = previous.get('result', {'ok': False, 'status': 'uncertain',
                                 'error': 'The same operation is already attempting; its outcome is unknown'})
                        return dict(result, action_id=previous['action_id'], target=previous['target'],
                                    draft_id=previous['draft_id'], replayed=True)
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
                    if name == 'select_session' and result.get('ok') and result.get('view_target'):
                        view_target = copy.deepcopy(result['view_target'])
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

        def dispatch(name, arguments):
            self.trace('tool_input', request_id=turn['request_id'], turn_id=turn['turn_id'], name=name, arguments=arguments)
            result = perform(name, arguments)
            self.trace('tool_result', request_id=turn['request_id'], turn_id=turn['turn_id'], name=name, result=result)
            self.state.tool_receipt(turn['turn_id'], name, result)
            return result

        def stream(delta):
            with self.control_lock:
                guard()
                if not isinstance(delta, str):
                    raise ValueError('Controller text delta must be text')
            if on_text:
                on_text(delta)

        try:
            if self.continuity_classifier:
                self.state.compact_continuity(self.continuity_classifier, turn['turn_id'])
            guard()
            context = self.state.context(turn['turn_id'])
            self.trace('provider_input', request_id=turn['request_id'], turn_id=turn['turn_id'], context=context, tools=self.tool_specs)
            result = self.provider(context, self.tool_specs, dispatch,
                                   on_text=stream, cancel=cancel)
            guard()
            if not isinstance(result, dict) or not isinstance(result.get('text'), str):
                raise ValueError('Controller provider returned no final text')
            self.trace('provider_result', request_id=turn['request_id'], turn_id=turn['turn_id'], result=result)
            if self.response_policy:
                result = self.response_policy(result, turn)
                guard()
            if view_target is not None:
                result['view_target'] = view_target
            receipt = self.receipt(self.state.finish_turn(turn['turn_id'], result=result))
            self.trace('canonical_receipt', request_id=turn['request_id'], turn_id=turn['turn_id'], receipt=receipt)
            return receipt
        except InterruptedError:
            self.state.cancel(turn['turn_id'])
            return self.receipt(self.state.snapshot()['turns'][turn['turn_id']])
        except Exception as error:
            result = self.response_policy({'text': ''}, turn) if self.response_policy else None
            return self.receipt(self.state.finish_turn(turn['turn_id'], result=result, error=error))
        finally:
            with self.control_lock:
                self.cancels.pop(turn['turn_id'], None)
