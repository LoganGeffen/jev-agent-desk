import argparse
import base64
import json
import os
import re
from pathlib import Path
import stat
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from visual_workspace import KINDS, MAX_FILE, MAX_FILES, MAX_VERSION, safe_components, safe_path


def read_file(path):
    absolute = Path(os.path.abspath(path))
    safe_components(absolute.parts)
    descriptors = []
    try:
        current = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(current)
        for part in absolute.parts[1:-1]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        fd = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE:
            raise ValueError('Capture needs regular files up to 8 MiB')
        data = bytearray()
        while len(data) <= MAX_FILE:
            chunk = os.read(fd, min(65536, MAX_FILE + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        if len(data) > MAX_FILE or (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise ValueError('Source changed during capture; retry after saving')
        return bytes(data)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def capture(path, entrypoint=None):
    path = Path(path).absolute()
    safe_components(path.parts)
    if path.is_symlink():
        raise ValueError('Symlinks cannot be captured')
    if path.is_dir():
        entrypoint = entrypoint or 'index.html'
        paths = []
        for directory, dirs, names in os.walk(path, followlinks=False):
            for name in dirs:
                child = Path(directory) / name
                if child.is_symlink() or name.startswith('.'):
                    raise ValueError('Bundles must not contain symlink or hidden directories; use an explicit export directory')
            for name in names:
                paths.append(Path(directory) / name)
                if len(paths) > MAX_FILES:
                    raise ValueError('Capture exceeds 128 files')
    else:
        entrypoint = entrypoint or path.name
        paths = [path]
        if path.suffix.lower() in KINDS['markdown']:
            source = read_file(path).decode('utf-8')
            for match in re.finditer(r'!\[[^\]]*\]\(<?([^\s)>]+)>?(?:\s+[^)]*)?\)', source):
                name = match.group(1)
                if urlsplit(name).scheme or name.startswith(('/', '#')):
                    continue
                safe_path(name)
                asset = path.parent / name
                if asset not in paths:
                    paths.append(asset)
                if len(paths) > MAX_FILES:
                    raise ValueError('Capture exceeds 128 files')
    files, total = [], 0
    for source in paths:
        relative = str(source.relative_to(path if path.is_dir() else path.parent))
        safe_path(relative)
        data = read_file(source)
        total += len(data)
        if total > MAX_VERSION:
            raise ValueError('Capture exceeds 16 MiB')
        files.append({'path': relative, 'data': base64.b64encode(data).decode()})
    return safe_path(entrypoint), files


def main():
    parser = argparse.ArgumentParser(description='Capture immutable creations and explicitly attach passive browser streams to Desk conversations.')
    parser.add_argument('--url', required=True, help='The private Desk URL')
    subs = parser.add_subparsers(dest='command', required=True)
    register = subs.add_parser('register')
    attach = subs.add_parser('attach')
    for command in (register, attach):
        command.add_argument('--tab', required=True)
        command.add_argument('--pane', required=True)
        command.add_argument('--identity', help='Captured exact pane identity; otherwise resolved once from Desk state')
    register.add_argument('--title', required=True)
    register.add_argument('--creation', help='Append a version to this creation UUID')
    register.add_argument('--kind', choices=KINDS)
    register.add_argument('--mode', choices=('captured', 'imported'), default='captured')
    register.add_argument('--entrypoint')
    register.add_argument('path')
    attach.add_argument('--name', required=True)
    attach.add_argument('--session', required=True)
    attach.add_argument('--port', required=True, type=int)
    attach.add_argument('--pid', required=True, type=int)
    detach = subs.add_parser('detach')
    detach.add_argument('--tab', required=True)
    detach.add_argument('--attachment', required=True)
    listing = subs.add_parser('list')
    listing.add_argument('--tab', required=True)
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.username or parsed.password or not parsed.hostname or (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1')):
        parser.error('Use a loopback HTTP or private HTTPS Desk origin')
    origin = args.url.rstrip('/')
    def request(route, body=None):
        headers = {'Origin': origin}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        req = Request(origin + route, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        with urlopen(req, timeout=20) as response:
            return json.load(response)
    if args.command == 'list':
        result = request('/api/visual/workspace?' + urlencode({'tab': args.tab}))
    elif args.command == 'detach':
        result = request('/api/visual/detach', {'tab': args.tab, 'attachment_id': args.attachment})
    else:
        identity = args.identity
        if not identity:
            state = request('/api/state')
            pane = next((pane for tab in state['tabs'] if tab['id'] == args.tab for pane in tab['panes'] if pane['id'] == args.pane), None)
            if pane is None or not pane.get('thread_id'):
                parser.error('The requested pane has no identified agent conversation')
            identity = pane['identity']
        target = {'tab': args.tab, 'pane': args.pane, 'identity': identity}
        if args.command == 'register':
            entrypoint, files = capture(args.path, args.entrypoint)
            kind = args.kind or next((kind for kind, extensions in KINDS.items() if Path(entrypoint).suffix.lower() in extensions), None)
            if kind is None:
                parser.error('Choose a supported creation entrypoint')
            body = {'target': target, 'title': args.title, 'kind': kind, 'mode': args.mode, 'entrypoint': entrypoint, 'files': files}
            if args.creation:
                body['creation_id'] = args.creation
        else:
            body = {'target': target, **{key: getattr(args, key) for key in ('name', 'session', 'port', 'pid')}}
        result = request('/api/visual/' + args.command, body)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
