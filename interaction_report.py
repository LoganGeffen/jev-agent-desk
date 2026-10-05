import argparse
from collections import defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys


IDS = ('session_id', 'request_id', 'turn_id', 'client_id', 'transcript_id', 'transcript_revision',
       'decision_id', 'playback_id', 'capture_id', 'generation', 'epoch', 'socket_epoch')
AUDIO_STAGES = {'progress', 'alignment', 'audio_queued', 'audio_ended', 'audio_playback'}
TRANSCRIPT_STAGES = {'turn_post', 'transcript_dispatch'}
SPEECH_STAGES = {'recognition', 'partial_transcript', 'committed_transcript', 'committed_transcript_with_timestamps'}
ANCHOR_STAGES = {'http_turn_input', 'provider_input', 'canonical_receipt', 'http_turn_receipt', 'turn_post'}


def read_events(path, max_bytes=16 * 1024 * 1024, max_events=20000, max_line=128000):
    path = Path(path)
    events, issues = [], []
    consumed = records = 0
    for source in (path.with_suffix(path.suffix + '.1'), path):
        if not source.exists():
            if source == path:
                issues.append({'source': str(source), 'problem': 'Current log missing'})
            continue
        try:
            with source.open('rb') as stream:
                line = 0
                while consumed < max_bytes and records < max_events:
                    raw = stream.readline(min(max_line + 1, max_bytes - consumed))
                    if not raw:
                        break
                    line += 1
                    records += 1
                    consumed += len(raw)
                    ref = {'path': str(source.resolve()), 'line': line}
                    if len(raw) > max_line or not raw.endswith(b'\n'):
                        issues.append({'source': ref, 'problem': 'Oversized or incomplete line omitted'})
                        while raw and not raw.endswith(b'\n') and consumed < max_bytes:
                            raw = stream.readline(min(max_line + 1, max_bytes - consumed))
                            consumed += len(raw)
                        continue
                    try:
                        event = json.loads(raw)
                        if not isinstance(event, dict) or not isinstance(event.get('stage'), str):
                            raise ValueError('Expected event object with stage')
                    except (ValueError, UnicodeDecodeError, RecursionError) as error:
                        issues.append({'source': ref, 'problem': 'Malformed event: ' + str(error)})
                        continue
                    events.append({'source': ref, 'event': event})
                if stream.read(1):
                    issues.append({'source': str(source), 'problem': 'Input byte/event limit reached; remaining events omitted'})
        except OSError as error:
            issues.append({'source': str(source), 'problem': type(error).__name__ + ': ' + str(error)})
    return events, issues


def scope(record):
    event = record['event']
    stage = event['stage']
    data = event.get('client', {}) if stage == 'browser' else event
    if not isinstance(data, dict):
        data = {}
    effective = data.get('stage', stage) if stage == 'browser' else event.get('kind', stage) if stage == 'voice_provider' else stage
    if not isinstance(effective, str):
        effective = stage
    scopes = [data]
    if stage == 'browser':
        context = data.get('context')
        if isinstance(context, dict):
            scopes.append({'session_id': context.get('session_id')})
    elif stage in ('canonical_receipt', 'http_turn_receipt'):
        scopes.append(event.get('receipt' if stage == 'canonical_receipt' else 'result', {}))
    elif stage in ('playback_ledger', 'rendition_input', 'rendition_result'):
        scopes.extend([event.get('request', {}), event.get('result', {})])
    ids, conflicts = {}, []
    for envelope in scopes:
        if not isinstance(envelope, dict):
            continue
        for key in IDS:
            value = envelope.get(key)
            if value is None:
                continue
            if key in ('generation', 'epoch', 'socket_epoch', 'transcript_revision'):
                if isinstance(value, bool) or not str(value).isdigit():
                    conflicts.append(key)
                    continue
                value = int(value)
            elif not isinstance(value, str) or not value:
                conflicts.append(key)
                continue
            if key in ids and ids[key] != value:
                conflicts.append(key)
            ids[key] = value
    return {'ids': ids, 'stage': effective, 'data': data, 'conflicts': conflicts}


def timestamp(record):
    value = record['event'].get('timestamp', '')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError, AttributeError):
        return float('-inf')


class Interactions:
    def __init__(self, events, issues=()):
        self.events, self.issues = events, list(issues)
        self.scopes = [scope(record) for record in events]
        self.requests = {}
        for record, item in zip(events, self.scopes):
            ids = item['ids']
            if ids.get('request_id') and not item['conflicts'] and item['stage'] in ANCHOR_STAGES:
                key = (ids.get('session_id'), ids['request_id'])
                self.requests.setdefault(key, {'session_id': key[0], 'request_id': key[1]})
        for key in list(self.requests):
            if key[0] is None and sum(other[1] == key[1] for other in self.requests if other[0]) == 1:
                del self.requests[key]
        self.clients = defaultdict(set)
        for item in self.scopes:
            ids = item['ids']
            if item['stage'] in ANCHOR_STAGES and ids.get('request_id') and ids.get('client_id'):
                for key in self.requests:
                    if key[1] == ids['request_id'] and (not ids.get('session_id') or key[0] == ids['session_id']):
                        self.clients[key].add(ids['client_id'])
        for record, item in zip(events, self.scopes):
            if timestamp(record) == float('-inf'):
                self.issues.append({'source': record['source'], 'problem': 'Missing or malformed timestamp; retained without time-based association'})
            if item['conflicts']:
                self.issues.append({'source': record['source'], 'problem': 'Conflicting or malformed correlation fields: ' + ', '.join(item['conflicts'])})
        self.owners = [set() for _ in events]
        self.turns, self.transcripts, self.decisions, self.playbacks, self.audio = [defaultdict(set) for _ in range(5)]
        for number, item in enumerate(self.scopes):
            ids = item['ids']
            if ids.get('request_id') and not item['conflicts']:
                candidates = {key for key in self.requests if key[1] == ids['request_id'] and
                              (not ids.get('session_id') or key[0] == ids['session_id'])}
                self.owners[number] = candidates
                if len(candidates) == 1:
                    self.bind(item, candidates)
        for _ in range(3):
            for number, item in enumerate(self.scopes):
                if item['conflicts']:
                    continue
                candidates = self.candidates(item)
                if self.owners[number]:
                    candidates |= self.owners[number]
                self.owners[number] = candidates
                if len(candidates) == 1:
                    self.bind(item, candidates)

    def bind(self, item, owners):
        ids, stage = item['ids'], item['stage']
        if ids.get('turn_id') and ids.get('request_id') and stage in ANCHOR_STAGES:
            self.turns[ids['turn_id']].update(owners)
        if stage == 'http_turn_input':
            if ids.get('decision_id'):
                self.decisions[ids['decision_id']].update(owners)
            if ids.get('client_id') and ids.get('transcript_id') and ids.get('transcript_revision') is not None:
                self.transcripts[(ids.get('session_id'), ids['client_id'], ids['transcript_id'], ids['transcript_revision'])].update(owners)
        if stage == 'playback_ledger' and ids.get('playback_id'):
            self.playbacks[ids['playback_id']].update(owners)
        if stage in {'turn_post', 'readback_source'} and ids.get('capture_id') and 'generation' in ids:
            self.audio[(ids['capture_id'], ids['generation'])].update(owners)

    def candidates(self, item):
        ids, stage = item['ids'], item['stage']
        candidates = set()
        if ids.get('turn_id'):
            candidates |= self.turns[ids['turn_id']]
        if ids.get('decision_id'):
            candidates |= self.decisions[ids['decision_id']]
        if ids.get('playback_id'):
            candidates |= self.playbacks[ids['playback_id']]
        if not ids.get('request_id') and ids.get('transcript_id') and stage in TRANSCRIPT_STAGES | {'turn_decision_input', 'turn_decision_result', 'pause_jev_input', 'pause_jev_result'}:
            for (session, client, transcript, revision), owners in self.transcripts.items():
                if transcript == ids['transcript_id'] and (not ids.get('client_id') or client == ids['client_id']) and (not ids.get('session_id') or session == ids['session_id']) and (ids.get('transcript_revision') is None or revision == ids['transcript_revision']):
                    candidates |= owners
        if (stage in AUDIO_STAGES or stage.startswith('tts_')) and ids.get('capture_id') and 'generation' in ids:
            candidates |= self.audio[(ids['capture_id'], ids['generation'])]
        return {key for key in candidates if (not ids.get('session_id') or key[0] == ids['session_id']) and
                (not ids.get('client_id') or not self.clients[key] or ids['client_id'] in self.clients[key])}

    def recent(self, limit=10):
        rows = []
        for key, request in self.requests.items():
            matched = [index for index, owners in enumerate(self.owners) if owners == {key}]
            last = max(matched, key=lambda index: (timestamp(self.events[index]), index), default=None)
            rows.append({**request, 'timestamp': self.events[last]['event'].get('timestamp') if last is not None else None,
                         'events': len(matched), 'turn_ids': sorted({self.scopes[index]['ids']['turn_id'] for index in matched if self.scopes[index]['ids'].get('turn_id')})})
        return sorted(rows, key=lambda row: row['timestamp'] or '', reverse=True)[:limit]

    def select(self, request_id, session_id=None):
        keys = [key for key in self.requests if (session_id is None or key[0] == session_id) and key[1] == request_id]
        if not keys:
            keys = [key for key in self.requests if (session_id is None or key[0] == session_id) and key[1].startswith(request_id)]
        if len(keys) != 1:
            raise ValueError('Request selection is ambiguous or missing; use an exact request ID and --session-id. Matches: ' + json.dumps(keys))
        key = keys[0]
        indices = [index for index, owners in enumerate(self.owners) if owners == {key} and not self.scopes[index]['conflicts']]
        captures = {self.scopes[index]['ids']['capture_id'] for index in indices if self.scopes[index]['ids'].get('capture_id')}
        shared, ambiguous = [], []
        for index, item in enumerate(self.scopes):
            if key in self.owners[index] and len(self.owners[index]) > 1:
                ambiguous.append(self.events[index]['source'])
            if item['ids'].get('capture_id') in captures and (item['stage'].startswith('stt_') or item['stage'] in SPEECH_STAGES | {'activation'}):
                shared.append(self.events[index])
        timeline = [self.events[index] for index in sorted(indices, key=lambda index: (timestamp(self.events[index]), index))]
        stages = {self.scopes[index]['stage'] for index in indices}
        gaps = []
        for stage, label in [('http_turn_input', 'Accepted HTTP request'), ('provider_input', 'Provider input'),
                             ('canonical_receipt', 'Canonical receipt')]:
            if stage not in stages:
                gaps.append(label + ' missing from selected evidence')
        if captures:
            gaps.append('Recognition has shared capture/socket scope, not exact utterance IDs; raw STT is contextual and not assigned by time or matching text.')
        if ambiguous:
            gaps.append('Conflicting/shared associations excluded; inspect ambiguous_sources.')
        truncated = [record['source'] for record in timeline if record['event'].get('truncated')]
        if truncated:
            gaps.append('Selected events were truncated; previews cannot reconstruct their missing fields.')
        if self.issues:
            gaps.append('Input is incomplete or malformed; inspect input_issues.')
        if not any(stage.startswith('tts_') for stage in stages):
            gaps.append('No uniquely associated TTS events; generation/capture or playback IDs may be absent.')
        unassigned = [{'source': record['source'], 'stage': item['stage']} for record, item, owners in zip(self.events, self.scopes, self.owners) if not owners or item['conflicts']]
        if unassigned:
            gaps.append(f'{len(unassigned)} events in the whole input have no usable association; they are not assumed to belong to this request. This can include rendition events without IDs.')
        gaps.append('Generated/queued audio does not prove it was heard; controller completion and submitted input do not prove agent task completion.')
        return {**self.requests[key], 'timeline': timeline, 'shared_capture_context': shared[:200],
                'shared_capture_events_omitted': max(0, len(shared) - 200), 'ambiguous_sources': ambiguous,
                'input_issues': self.issues, 'gaps': gaps, 'unassigned_sources': unassigned[:200], 'unassigned_sources_omitted': max(0, len(unassigned) - 200), 'evidence': evidence(timeline)}


def evidence(timeline):
    result = {'accepted_text': [], 'decisions': [], 'delivery': [], 'controller_results': [], 'originals': [], 'renditions': [], 'playback': []}
    seen_actions = set()
    for record in timeline:
        event, source = record['event'], record['source']
        item = scope(record)
        stage, data = item['stage'], item['data']
        if stage in ('http_turn_input', 'transcript_dispatch'):
            result['accepted_text'].append({'text': data.get('text'), 'stage': stage, 'source': source})
        if stage in ('turn_decision_result', 'authorization_result'):
            result['decisions'].append({'stage': stage, 'decision': data.get('decision'), 'source': source})
        if stage in ('canonical_receipt', 'http_turn_receipt'):
            receipt = event.get('receipt', event.get('result', {}))
            if not isinstance(receipt, dict):
                continue
            result['controller_results'].append({'status': receipt.get('status'), 'text': receipt.get('text'), 'source': source,
                                                'meaning': 'Controller turn outcome, never agent task completion'})
            for field in ('readback', 'available_readback'):
                readback = receipt.get(field)
                if isinstance(readback, dict):
                    result['originals'].append({'original': readback.get('original'), 'role': field, 'source': source})
            for action in receipt.get('actions', []):
                if not isinstance(action, dict):
                    continue
                action_id = action.get('action_id')
                if action_id and action_id in seen_actions:
                    continue
                if action_id:
                    seen_actions.add(action_id)
                result['delivery'].append({'action_id': action_id, 'tool': action.get('tool'), 'status': action.get('status'),
                    'target': action.get('target'), 'payload': action.get('payload'), 'source': source,
                    'meaning': 'Application delivery/control receipt; submitted is not agent task completion'})
        if stage == 'rendition_result':
            result['renditions'].append({'result': event.get('result'), 'source': source})
        if stage == 'playback_ledger':
            receipt = event.get('result', {})
            if not isinstance(receipt, dict) or receipt.get('error'):
                continue
            boundary = receipt.get('full_boundary_id')
            full = receipt.get('status') == 'completed' and bool(boundary) and receipt.get('completed_phrases') == [boundary]
            result['playback'].append({'playback_id': receipt.get('playback_id'), 'status': receipt.get('status'),
                'full_acknowledgment_recorded': full, 'heard_prefix': receipt.get('heard_prefix'), 'source': source,
                'meaning': 'Recorded app acknowledgment, not independently measured physical hearing'})
    return result


def readable(report):
    if 'interactions' in report:
        lines = ['Recent interactions (explicit request anchors):']
        lines += [f"{row['timestamp'] or '?'}  {row['request_id']}  session={row['session_id'] or '?'}  events={row['events']}" for row in report['interactions']]
        if not report['interactions']:
            lines.append('No interactions recorded in the available log.')
        lines += ['Input issue: ' + json.dumps(issue) for issue in report['input_issues']]
        return '\n'.join(lines) + '\n'
    lines = [f"Request {report['request_id']}  session={report['session_id'] or '?'}"]
    for category, items in report['evidence'].items():
        if items:
            lines.append(category.replace('_', ' ').capitalize() + ':')
            for item in items:
                source = item['source']
                lines.append('  ' + json.dumps({key: value for key, value in item.items() if key != 'source'}, ensure_ascii=False) + f" [{source['path']}:{source['line']}]")
    lines.append('Selected timeline (full events in JSON):')
    for record in report['timeline']:
        item = scope(record)
        event = record['event']
        source = record['source']
        lines.append(f"{event.get('timestamp', '?')} {item['stage']} [{source['path']}:{source['line']}]")
        data = item['data']
        detail = data.get('text') or data.get('name') or data.get('kind')
        if detail:
            lines.append('  ' + json.dumps(str(detail)[:200], ensure_ascii=False))
    lines.append(f"Shared capture context: {len(report['shared_capture_context'])} events, not assigned to this utterance (full records in JSON).")
    lines += ['Gap: ' + gap for gap in report['gaps']]
    lines += ['Ambiguous source: ' + json.dumps(source) for source in report['ambiguous_sources']]
    lines += ['Unassigned source (whole input): ' + json.dumps(source) for source in report['unassigned_sources']]
    lines += ['Input issue: ' + json.dumps(issue) for issue in report['input_issues']]
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only private interaction reconstruction; no provider or agent effects.')
    parser.add_argument('--log', type=Path, required=True, help='pipeline.jsonl; also reads pipeline.jsonl.1')
    parser.add_argument('--request', help='Exact request ID or unambiguous prefix')
    parser.add_argument('--session-id')
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--output', type=Path, help='Create a new private report file; never overwrite')
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100:
        parser.error('--limit must be between 1 and 100')
    events, issues = read_events(args.log)
    interactions = Interactions(events, issues)
    try:
        report = interactions.select(args.request, args.session_id) if args.request else {'interactions': interactions.recent(args.limit), 'input_issues': issues}
        output = json.dumps(report, ensure_ascii=False, indent=2) + '\n' if args.json else readable(report)
        if args.output:
            args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(output)
        else:
            sys.stdout.write(output)
    except (ValueError, OSError) as error:
        parser.exit(2, str(error) + '\n')
    return 0


if __name__ == '__main__':
    main()
