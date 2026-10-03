import copy
import threading
import uuid

import jev


class JarvisTurns:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.clients = {}
        self.requests = {}
        self.retired = set()

    def decide(self, body):
        for key in ('client_id', 'transcript_id'):
            if not isinstance(body.get(key), str) or not body[key]:
                raise ValueError('A transcript and client identity are required')
        revision = body.get('transcript_revision')
        if type(revision) is not int or revision < 0 or not isinstance(body.get('text'), str):
            raise ValueError('An exact transcript and nonnegative revision are required')
        if type(body.get('invalidate', False)) is not bool:
            raise ValueError('invalidate must be a boolean')
        subject = self.app.jarvis.snapshot()['subject']
        if body.get('subject') != subject:
            raise ValueError('Conversational subject changed')
        record = {key: copy.deepcopy(body[key]) for key in ('client_id', 'transcript_id', 'transcript_revision', 'text')}
        record.update(subject=subject, session_id=self.app.jarvis.state.session_id,
                      decision='wait', correction=False, generation=uuid.uuid4().hex)
        with self.lock:
            old = self.clients.get(body['client_id'])
            if (record['client_id'], record['transcript_id']) in self.retired:
                raise ValueError('Transcript was superseded')
            if old and old['transcript_id'] != record['transcript_id']:
                self.retired.add((old['client_id'], old['transcript_id']))
            if old and old['transcript_id'] == record['transcript_id'] and revision < old['transcript_revision']:
                raise ValueError('Transcript revision is stale')
            self.clients[body['client_id']] = record
        if not body.get('invalidate') and body['text'].strip():
            try:
                answers = self.app.evaluate(jev.turn_decision_question(body['text'], subject))['answers']
                decision = answers['decision']['choice']
                correction = answers['correction']['choice'] == 'yes'
                if decision not in ('complete', 'wait', 'cancel'):
                    raise ValueError('Unknown turn decision')
            except Exception:
                decision, correction = 'wait', False
            with self.lock:
                if self.clients[body['client_id']] is not record or self.app.jarvis.snapshot()['subject'] != subject:
                    return {**self.projection(record), 'decision': 'wait', 'reason': 'Transcript or subject changed'}
                record.update(decision=decision, correction=correction)
                if decision == 'complete':
                    record['decision_id'] = uuid.uuid4().hex
        return self.projection(record)

    def projection(self, record):
        return copy.deepcopy({key: value for key, value in record.items() if key not in ('text', 'generation')})

    def commit(self, body):
        explicit = body.get('explicit_send', False)
        if type(explicit) is not bool:
            raise ValueError('explicit_send must be a boolean')
        if body.get('source', 'typed') == 'typed' or explicit:
            return
        request_id = body['request_id']
        with self.lock:
            if request_id in self.app.jarvis.snapshot()['requests']:
                return
            record = self.clients.get(body.get('client_id'))
            if not record or record['decision'] != 'complete' or not record.get('decision_id'):
                raise ValueError('Automatic voice send needs a current complete decision')
            expected = ('client_id', 'transcript_id', 'transcript_revision', 'decision_id', 'text')
            if any(body.get(key) != record[key] for key in expected) or record['subject'] != self.app.jarvis.snapshot()['subject']:
                raise ValueError('Automatic voice decision is stale or belongs to different words')
            if record.get('request_id') and record['request_id'] != request_id:
                raise ValueError('Voice decision already belongs to another request')
            record['request_id'] = request_id
            self.requests[request_id] = record

    def guard(self, request_id):
        with self.lock:
            record = self.requests.get(request_id)
            if record:
                snapshot = self.app.jarvis.snapshot()
                turn = snapshot['turns'].get(snapshot['requests'].get(request_id))
                if self.clients.get(record['client_id']) is not record or turn and turn['subject'] != record['subject']:
                    raise InterruptedError('Automatic voice authorization was invalidated')

    def create_playback(self, body):
        state = self.app.jarvis.state
        turn = state.snapshot()['turns'].get(body['turn_id'])
        if not turn or turn['status'] != 'completed':
            raise ValueError('Playback requires a completed application turn')
        if body.get('style', 'original') not in ('original', 'spoken'):
            raise ValueError('Unknown playback style')
        result = turn.get('result', {})
        readback = result.get('readback')
        original = readback['original'] if readback else result.get('text', '')
        if not original:
            raise ValueError('This turn has no verified text to play')
        text = original
        playback = state.create_playback(body['turn_id'], body['client_id'], body['epoch'], text,
                                         readback.get('reply_ref') if readback else None,
                                         source_ref={'turn_id': body['turn_id'], 'original': original,
                                                     'style': body.get('style', 'original')})
        if body.get('style') == 'spoken' and readback:
            with state.lock:
                stored = state.playbacks[playback['playback_id']]
                stored.update(text=None, rendition_pending=True)
                state.changed()
                playback = copy.deepcopy(stored)
        return playback

    def update_playback(self, body):
        state = self.app.jarvis.state
        current = state.snapshot()['playbacks'].get(body['playback_id'])
        if not current or current['client_id'] != body['client_id'] or current['epoch'] != body['epoch'] or current['status'] in ('completed', 'cancelled', 'failed'):
            raise ValueError('Playback acknowledgment has a stale owner or ended playback')
        if current and current.get('rendition_pending') and body['status'] == 'completed' and body.get('completed_phrases'):
            with self.app.readbacks.lock:
                entry = self.app.readbacks.entries.get((current['source_ref']['original'], 'spoken'))
                result = copy.deepcopy(entry['result']) if entry and entry['done'] and not entry['error'] else None
            if not result or not isinstance(result.get('text'), str):
                raise ValueError('Spoken rendition is not yet app-confirmed')
            with state.lock:
                stored = state.playbacks[body['playback_id']]
                stored.update(text=result['text'], rendition_pending=False)
                state.changed()
        playback = self.app.jarvis.state.update_playback(body['playback_id'], body['client_id'], body['epoch'],
                       body['status'], body.get('completed_phrases', []))
        if playback['status'] == 'completed' and playback['completed_phrases'] == [playback.get('full_boundary_id')] and playback.get('reply_ref'):
            self.app.heard_reply(playback['reply_ref'])
        return playback
