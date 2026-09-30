import argparse
from datetime import datetime
from getpass import getpass
import json
import os
import re
import signal
import shutil
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

from server import Playground, tmux
from pane_identity import CODEX_IDENTITY_CONFIG
import claude_runtime


ROOT = Path(__file__).resolve().parent
RUN = ROOT / ".run"
OWNER = RUN / "owner.json"
ATTACH_RUN = RUN / "attached"
LABELS = ["Alpha", "Beta", "JEV controller", "JEV testing"]


def command(*args):
    return "exec " + shlex.join([sys.executable, *map(str, args)])


def fixtures(socket):
    for index, label in enumerate(LABELS):
        process = command(ROOT / "launch.py", "clock", label)
        if index == 0:
            tmux(socket, "-f", "/dev/null", "new-session", "-d", "-s", "tabs", "-n", label,
                 "-x", "100", "-y", "24", process)
            tmux(socket, "set-option", "-g", "automatic-rename", "off")
            tmux(socket, "set-option", "-g", "status-left", "Jev playground | ")
        else:
            tmux(socket, "new-window", "-d", "-t", "tabs:", "-n", label, process)


def status():
    owner = json.loads(OWNER.read_text())
    pid = tmux(owner["socket"], "display-message", "-p", "#{pid}")
    if pid != owner["pid"]:
        raise RuntimeError("Socket does not belong to the recorded playground server")
    return owner


def start():
    if OWNER.exists():
        raise RuntimeError("A run is already recorded; use status or stop before starting again")
    RUN.mkdir(mode=0o700, exist_ok=True)
    socket = str(Path(tempfile.mkdtemp(prefix="jev-tabs-")) / "tmux.sock")
    fixtures(socket)
    owner = {"socket": socket, "pid": tmux(socket, "display-message", "-p", "#{pid}")}
    OWNER.write_text(json.dumps(owner))
    (RUN / "url").unlink(missing_ok=True)
    tmux(socket, "new-session", "-d", "-s", "web", "-n", "server",
         command(ROOT / "server.py", "--socket", socket, "--run-dir", RUN))
    for _ in range(100):
        if (RUN / "url").exists():
            url = (RUN / "url").read_text()
            with urlopen(url + "/api/state", timeout=2) as response:
                state = json.load(response)
            print(f"Page: {url}\nWatch: tmux -S {shlex.quote(socket)} attach -t tabs")
            print(f"Jev configured: {state['api_configured']}\nEvents: {RUN / 'events.jsonl'}")
            return
        time.sleep(0.1)
    raise RuntimeError(f"Web server did not start. Inspect: tmux -S {socket} capture-pane -pt web")


def stop():
    owner = status()
    tmux(owner["socket"], "kill-server")
    socket = Path(owner["socket"])
    socket.unlink(missing_ok=True)
    socket.parent.rmdir()
    OWNER.unlink()
    (RUN / "url").unlink(missing_ok=True)
    print("Stopped this playground's tmux server and web process. Logs retained in .run/.")


def process_started(pid):
    try:
        stat = (Path('/proc') / str(pid) / 'stat').read_text()
    except OSError:
        return None
    return stat.rsplit(')', 1)[1].split()[19]


def attach_owner(run_dir):
    return Path(run_dir) / 'owner.json'


def attach_status(run_dir=ATTACH_RUN):
    run_dir = Path(run_dir).resolve()
    owner_path = attach_owner(run_dir)
    if not owner_path.exists():
        raise RuntimeError('No attached web pilot is recorded')
    owner = json.loads(owner_path.read_text())
    pid = int(owner['pid'])
    if process_started(pid) != owner.get('started'):
        raise RuntimeError('The recorded attached web process is no longer running')
    try:
        command = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0')
    except OSError:
        raise RuntimeError('The recorded attached web process is no longer running') from None
    if str(ROOT / 'server.py').encode() not in command or str(run_dir).encode() not in command:
        raise RuntimeError('The recorded process does not belong to this attached web pilot')
    url_path = run_dir / 'url'
    return {**owner, 'url': url_path.read_text().strip() if url_path.exists() else None}


def private_web_environment():
    environment = dict(os.environ)
    missing = {name for name in ('TYPESAFE_API_KEY', 'ELEVENLABS_API_KEY', 'ELEVENLABS_VOICE_ID')
               if not environment.get(name)}
    if not missing or not OWNER.exists():
        return environment
    try:
        private = status()
        pid = tmux(private['socket'], 'display-message', '-p', '-t', '=web:', '#{pane_pid}')
        values = (Path('/proc') / pid / 'environ').read_bytes().split(b'\0')
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return environment
    for value in values:
        key, separator, raw = value.partition(b'=')
        name = key.decode(errors='ignore')
        if separator and name in missing:
            environment[name] = raw.decode()
    return environment


def attached_environment(run_dir):
    owner = attach_status(run_dir)
    environment = dict(os.environ)
    names = {'TYPESAFE_API_KEY', 'ELEVENLABS_API_KEY', 'ELEVENLABS_VOICE_ID'}
    values = (Path('/proc') / str(owner['pid']) / 'environ').read_bytes().split(b'\0')
    if attach_status(run_dir)['started'] != owner['started']:
        raise RuntimeError('Authentication source changed')
    for value in values:
        key, separator, raw = value.partition(b'=')
        name = key.decode(errors='ignore')
        if separator and name in names and not environment.get(name):
            environment[name] = raw.decode()
    return environment


def spawn_attached_web(command_line, environment, log_path):
    input_fd = os.open(os.devnull, os.O_RDONLY)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        setsid = shutil.which('setsid')
        if not setsid:
            raise RuntimeError('setsid is required to launch the attached web pilot')
        return os.posix_spawn(
            setsid, [setsid, *command_line], environment,
            file_actions=[
                (os.POSIX_SPAWN_DUP2, input_fd, 0),
                (os.POSIX_SPAWN_DUP2, log_fd, 1),
                (os.POSIX_SPAWN_DUP2, log_fd, 2),
                (os.POSIX_SPAWN_CLOSE, input_fd),
                (os.POSIX_SPAWN_CLOSE, log_fd),
            ],
        )
    finally:
        os.close(input_fd)
        os.close(log_fd)


def stop_attached_process(pid, started):
    if started is None or process_started(pid) != started:
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        try:
            reaped = os.waitpid(pid, os.WNOHANG)[0] == pid
        except ChildProcessError:
            reaped = False
        if reaped or process_started(pid) != started:
            return
        time.sleep(.1)
    os.kill(pid, signal.SIGKILL)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def attach_start(socket, source_session, view_session, run_dir=ATTACH_RUN, port=0,
                 ask_key=False, reuse_private_auth=True, environment=None, public_origin=None,
                 voice_port=0, independent_selection=False):
    run_dir = Path(run_dir).resolve()
    owner_path = attach_owner(run_dir)
    if owner_path.exists():
        attach_status(run_dir)
        raise RuntimeError('An attached web pilot is already running')
    Playground(socket, run_dir / 'events.jsonl', source_session=source_session,
               view_session=view_session)
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (run_dir / 'url').unlink(missing_ok=True)
    if environment is None:
        environment = private_web_environment() if reuse_private_auth else dict(os.environ)
    if ask_key:
        environment['TYPESAFE_API_KEY'] = getpass('TypeSafe API key (hidden): ')
    command_line = [sys.executable, str(ROOT / 'server.py'), '--socket', socket,
                    '--source-session', source_session, '--view-session', view_session,
                    '--run-dir', str(run_dir), '--port', str(port), '--redact-terminal-logs']
    if public_origin:
        command_line.extend(['--public-origin', public_origin])
    command_line.extend(['--voice-port', str(voice_port)])
    if independent_selection:
        command_line.append('--independent-selection')
    pid = spawn_attached_web(command_line, environment, run_dir / 'web.log')
    started = process_started(pid)
    if started is None:
        stop_attached_process(pid, started)
        raise RuntimeError('Attached web process exited before its identity could be recorded')
    owner = {'kind': 'attached-web', 'pid': pid, 'started': started,
             'socket': socket, 'source_session': source_session, 'view_session': view_session,
             'port': port, 'voice_port': voice_port, 'public_origin': public_origin,
             'independent_selection': independent_selection}
    temporary = owner_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(owner, indent=2) + '\n')
    temporary.replace(owner_path)
    try:
        for _ in range(100):
            url_path = run_dir / 'url'
            if url_path.exists():
                url = url_path.read_text().strip()
                with urlopen(url + '/api/state', timeout=2) as response:
                    state = json.load(response)
                if state['target'] != {'source_session': source_session,
                                       'view_session': view_session}:
                    raise RuntimeError('Attached web server reported a different tmux target')
                print(f"Page: {url}\nSource: {source_session}\nView: {view_session}")
                print(f"Jev configured: {state['api_configured']}\nVoice configured: {state['voice']['configured']}")
                print(f"Stop: {sys.executable} {ROOT / 'launch.py'} attach-stop --run-dir {run_dir}")
                return state
            if process_started(pid) != started:
                break
            time.sleep(.1)
        raise RuntimeError(f'Attached web server did not start. Inspect: {run_dir / "web.log"}')
    except BaseException:
        stop_attached_process(pid, started)
        owner_path.unlink(missing_ok=True)
        (run_dir / 'url').unlink(missing_ok=True)
        raise


def attach_stop(run_dir=ATTACH_RUN):
    run_dir = Path(run_dir).resolve()
    owner = attach_status(run_dir)
    pid = int(owner['pid'])
    stop_attached_process(pid, owner['started'])
    attach_owner(run_dir).unlink()
    (run_dir / 'url').unlink(missing_ok=True)
    print('Stopped only the attached Jev web process. The target tmux server was not changed.')


def attach_restart(run_dir):
    owner = attach_status(run_dir)
    environment = attached_environment(run_dir)
    attach_stop(run_dir)
    return attach_start(owner['socket'], owner['source_session'], owner['view_session'], run_dir,
                        port=owner.get('port', 0), environment=environment,
                        public_origin=owner.get('public_origin'), voice_port=owner.get('voice_port', 0),
                        independent_selection=owner.get('independent_selection', False))


def add_codex(label):
    socket = status()["socket"]
    workspace = tempfile.mkdtemp(prefix="jev-codex-")
    process = "exec " + shlex.join([
        "env", "-u", "TYPESAFE_API_KEY", "-u", "ELEVENLABS_API_KEY", "codex", "--no-daemon", "--no-alt-screen",
        "--sandbox", "read-only", "--ask-for-approval", "never",
        "-m", "gpt-5.6-luna", "-c", "model_reasoning_effort=low",
        *CODEX_IDENTITY_CONFIG,
    ])
    window, pane = tmux(socket, "new-window", "-d", "-P", "-F", "#{window_id}\t#{pane_id}",
                        "-t", "tabs:", "-n", label, "-c", workspace, process).split("\t")
    print(f"Starting {label}: {window} / {pane} in {workspace}", flush=True)

    def wait_screen(pattern):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            screen = tmux(socket, "capture-pane", "-p", "-t", pane)
            match = re.search(pattern, screen)
            if match:
                return match
            time.sleep(.1)
        raise TimeoutError(f"Inspect the new Codex pane {pane}; startup did not reach {pattern}")

    wait_screen("Trust and continue")
    tmux(socket, "send-keys", "-t", pane, "Enter")
    wait_screen("GPT-5.6-Luna low")
    tmux(socket, "send-keys", "-t", pane, "-l", "/status")
    time.sleep(.2)
    tmux(socket, "send-keys", "-t", pane, "Enter")
    thread = wait_screen(r"Session:\s+([0-9a-f-]{36})").group(1)
    filename = RUN / "agents.json"
    agents = json.loads(filename.read_text()) if filename.exists() else {}
    agents = {key: value for key, value in agents.items() if value["socket"] == socket}
    agents[window] = {"socket": socket, "window_id": window, "pane_id": pane,
                      "thread_id": thread, "workspace": workspace}
    temporary = filename.with_suffix(".tmp")
    temporary.write_text(json.dumps(agents, indent=2) + "\n")
    temporary.replace(filename)
    print(f"Registered {label}: {thread}")


def add_claude(label):
    socket = status()['socket']
    workspace = tempfile.mkdtemp(prefix='jev-claude-')
    window, pane = tmux(socket, 'new-window', '-d', '-P', '-F', '#{window_id}\t#{pane_id}',
                        '-t', 'tabs:', '-n', label, '-c', workspace,
                        claude_runtime.command(socket, RUN)).split('\t')
    print(f'Starting {label}: {window} / {pane} in {workspace}', flush=True)
    for _ in range(100):
        pid = tmux(socket, 'display-message', '-p', '-t', pane, '#{pane_pid}')
        process = claude_runtime.process_identity(pid)['process']
        binding = claude_runtime.read_binding(RUN, process)
        if binding:
            print(f"Tracked {label}: {binding['session_id']}", flush=True)
            return
        time.sleep(.2)
    raise TimeoutError(f'Claude identity did not appear; inspect sandbox pane {pane}')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status", "clock", "add-codex", 'add-claude',
                                           'attach-start', 'attach-stop', 'attach-status', 'attach-restart'])
    parser.add_argument("label", nargs="?")
    parser.add_argument("--ask-key", action="store_true", help="Prompt privately; keep key in process environment only")
    parser.add_argument('--socket', required=False)
    parser.add_argument('--source-session', default='main')
    parser.add_argument('--view-session')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--run-dir', type=Path, default=ATTACH_RUN)
    parser.add_argument('--auth-from-run', type=Path)
    parser.add_argument('--public-origin')
    parser.add_argument('--voice-port', type=int, default=0)
    parser.add_argument('--independent-selection', action='store_true')
    args = parser.parse_args()
    if args.action == "clock":
        while True:
            print(f"\033[2J\033[H{args.label}\n\n{datetime.now().isoformat(timespec='seconds')}\n\n"
                  "Disposable Jev tab fixture — no agent session attached.", flush=True)
            time.sleep(1)
    else:
        os.umask(0o077)
        if args.action == "start" and args.ask_key:
            os.environ["TYPESAFE_API_KEY"] = getpass("TypeSafe API key (hidden): ")
        if args.action == "status":
            print(json.dumps(status(), indent=2))
            print((RUN / "url").read_text())
        elif args.action == 'attach-start':
            if not args.socket:
                parser.error('attach-start requires --socket (use tmux display-message -p \"#{socket_path}\")')
            attach_start(args.socket, args.source_session, args.view_session or args.source_session,
                         run_dir=args.run_dir, port=args.port, ask_key=args.ask_key,
                         environment=attached_environment(args.auth_from_run) if args.auth_from_run else None,
                         public_origin=args.public_origin, voice_port=args.voice_port,
                         independent_selection=args.independent_selection)
        elif args.action == 'attach-stop':
            attach_stop(args.run_dir)
        elif args.action == 'attach-status':
            print(json.dumps(attach_status(args.run_dir), indent=2))
        elif args.action == 'attach-restart':
            attach_restart(args.run_dir)
        elif args.action == "add-codex":
            add_codex(args.label or "Luna")
        elif args.action == 'add-claude':
            add_claude(args.label or 'Claude')
        else:
            {"start": start, "stop": stop}[args.action]()
