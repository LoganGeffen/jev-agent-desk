import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from managed_provider import INSTRUCTIONS, read_record
from visual import capture
from visual_workspace import KINDS, MAX_BODY, safe_path


def schema(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {}, 'required': list(required), 'additionalProperties': False}


STRING = {'type': 'string'}
TOOLS = [
    {'name': 'desk_context', 'description': 'Read user-selected immutable output reference context for this conversation at the start of each request.', 'inputSchema': schema()},
    {'name': 'desk_browser_open', 'description': 'Open an owned browser page visible beside this conversation, using the configured persistent Desk account.', 'inputSchema': schema({'url': STRING}, ['url'])},
    {'name': 'desk_browser_downloads', 'description': 'Finish saving completed browser downloads. If pending_downloads is nonzero, check again before claiming the file is saved. This never retries the original click.', 'inputSchema': schema()},
    {'name': 'desk_browser_pages', 'description': 'List only this conversation browser pages and their current handles/epochs.', 'inputSchema': schema()},
    {'name': 'desk_browser_action', 'description': 'Control one exact owned page. Snapshot refreshes the account epoch after human sign-in. Agent calls pause during Sign in; never replay an uncertain mutation.', 'inputSchema': schema({'page_id': STRING, 'run_id': STRING, 'account_epoch': {'type': 'integer'}, 'action': {'type': 'string', 'enum': ['navigate', 'snapshot', 'click', 'fill', 'press', 'scroll', 'back', 'forward', 'reload', 'close', 'screenshot']}, 'args': {'type': 'object'}}, ['page_id', 'run_id', 'account_epoch', 'action'])},
    {'name': 'desk_publish_file', 'description': 'Publish a completed file or static export directory from this workspace to Desk and return its immutable saved version receipt. Use creation_id to revise an existing creation.', 'inputSchema': schema({'path': STRING, 'title': STRING, 'entrypoint': STRING, 'creation_id': STRING, 'receipt_key': STRING}, ['path', 'title'])},
    {'name': 'desk_create_document', 'description': 'Create and publish a completed Markdown, HTML or text document in one operation. Automatically opens its saved immutable preview in this conversation.', 'inputSchema': schema({'path': STRING, 'title': STRING, 'text': STRING, 'creation_id': STRING, 'receipt_key': STRING}, ['path', 'title', 'text'])},
    {'name': 'desk_app_start', 'description': 'Start a development app in this workspace and open an isolated app browser when ready. command is argv, must use {port}, and bind explicitly to 127.0.0.1. Never include credentials in command arguments.', 'inputSchema': schema({'title': STRING, 'command': {'type': 'array', 'items': STRING}, 'cwd': STRING, 'ready_path': STRING, 'build_identity': STRING}, ['title', 'command'])},
    {'name': 'desk_app_state', 'description': 'Read readiness, bounded error output, and browser handles for this conversation apps.', 'inputSchema': schema()},
    {'name': 'desk_app_stop', 'description': 'Stop this conversation managed app; immutable saved exports remain available.', 'inputSchema': schema({'app_id': STRING}, ['app_id'])},
]


class Bridge:
    def __init__(self, run_dir, session_id):
        self.run_dir = Path(run_dir)
        self.session_id = session_id
        self.record = read_record(run_dir, session_id)

    def path(self, value):
        if not isinstance(value, str):
            raise ValueError('A workspace-relative path is required')
        root = Path(self.record['workspace'])
        path = Path(os.path.abspath(root / value))
        if not path.is_relative_to(root) or path.resolve() != path:
            raise ValueError('Publication must stay inside its managed workspace without symlinks')
        return path

    def call(self, tool, args):
        if tool in ('desk_publish_file', 'desk_create_document'):
            path = self.path(args['path'])
            if tool == 'desk_create_document':
                safe_path(str(path.relative_to(self.record['workspace'])))
                if path.suffix.lower() not in {'.html', '.htm', '.md', '.markdown', '.txt', '.csv'}:
                    raise ValueError('Create document supports HTML, Markdown and text')
                data = args['text'].encode()
                if len(data) > 8 * 1024 * 1024:
                    raise ValueError('Document exceeds 8 MiB')
                descriptors = [os.open(self.record['workspace'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
                try:
                    for part in path.relative_to(self.record['workspace']).parts[:-1]:
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=descriptors[-1])
                        except FileExistsError:
                            pass
                        descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[-1]))
                    fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=descriptors[-1])
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(data)
                finally:
                    for fd in reversed(descriptors):
                        os.close(fd)
            entrypoint, files = capture(path, args.get('entrypoint'))
            kind = next((kind for kind, extensions in KINDS.items() if Path(entrypoint).suffix.lower() in extensions), None)
            if kind is None:
                raise ValueError('Unsupported output format')
            payload = {'title': args['title'], 'kind': kind, 'entrypoint': entrypoint, 'files': files}
            if args.get('creation_id'):
                payload['creation_id'] = args['creation_id']
            payload['receipt_key'] = args.get('receipt_key') or hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            args, tool = payload, 'desk_publish_file'
        origin = (self.run_dir / 'url').read_text().strip()
        if not origin.startswith('http://127.0.0.1:'):
            raise ValueError('Managed tools require the private loopback Desk service')
        body = json.dumps({'session_id': self.session_id, 'tool': tool, 'arguments': args}).encode()
        if len(body) > MAX_BODY:
            raise ValueError('Tool request exceeds capture limit')
        request = Request(origin + '/api/provider/tool', data=body, headers={'Origin': origin, 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.record['token']})
        try:
            with urlopen(request, timeout=45) as response:
                return json.load(response)
        except HTTPError as error:
            detail = json.loads(error.read())
            raise ValueError(detail.get('error', 'Desk tool failed')) from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--session-id', required=True)
    args = parser.parse_args()
    bridge = Bridge(args.run_dir, args.session_id)
    for line in sys.stdin:
        message = json.loads(line)
        identifier = message.get('id')
        if identifier is None:
            continue
        try:
            method = message['method']
            if method == 'initialize':
                result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'desk', 'version': '1.0.0'}, 'instructions': INSTRUCTIONS}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': TOOLS}
            elif method == 'tools/call':
                params = message['params']
                if params['name'] not in {tool['name'] for tool in TOOLS}:
                    raise ValueError('Unknown Desk tool')
                value = bridge.call(params['name'], params.get('arguments', {}))
                image = value.get('result') if isinstance(value, dict) else None
                if isinstance(image, dict) and image.get('mime') in ('image/jpeg', 'image/png') and image.get('data'):
                    result = {'content': [{'type': 'text', 'text': json.dumps({key: item for key, item in value.items() if key != 'result'})},
                                          {'type': 'image', 'mimeType': image['mime'], 'data': image['data']}]}
                else:
                    result = {'content': [{'type': 'text', 'text': json.dumps(value)}]}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': identifier, 'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
                continue
        except Exception as error:
            result = {'isError': True, 'content': [{'type': 'text', 'text': str(error)}]}
        print(json.dumps({'jsonrpc': '2.0', 'id': identifier, 'result': result}), flush=True)


if __name__ == '__main__':
    main()
