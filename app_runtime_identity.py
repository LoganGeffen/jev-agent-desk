import os
from pathlib import Path


def listener_identity(pid, port):
    try:
        process = str(pid) + ':' + Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        cwd = os.readlink(f'/proc/{pid}/cwd')
        pending, inodes = [str(pid)], set()
        while pending:
            current = pending.pop()
            proc = Path('/proc') / current
            try:
                pending.extend((proc / 'task' / current / 'children').read_text().split())
                for fd in (proc / 'fd').iterdir():
                    try:
                        link = os.readlink(fd)
                        if link.startswith('socket:['):
                            inodes.add(link[8:-1])
                    except OSError:
                        pass
            except OSError:
                pass
        found = []
        for row in Path('/proc/net/tcp').read_text().splitlines()[1:]:
            cells = row.split()
            if cells[1] == f'0100007F:{port:04X}' and cells[3] == '0A' and cells[9] in inodes:
                found.append(cells[9])
        if len(found) == 1:
            return {'pid': pid, 'port': port, 'process': process, 'cwd': cwd, 'inode': found[0]}
    except (OSError, ValueError):
        pass
    return None


def listener_matches(binding):
    return isinstance(binding, dict) and listener_identity(binding.get('pid'), binding.get('port')) == binding
