import json
import os
import re
import select
import subprocess
import time
from pane_identity import process_identity


def tmux(socket, *args, input=None):
    result = subprocess.run(
        ["tmux", "-S", str(socket), *args], input=input,
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "tmux command failed")
    return result.stdout.rstrip("\n")


def focus(socket, window, pane=None, session="tabs"):
    tmux(socket, "select-window", "-t", f"{session}:{window}")
    if pane is not None:
        tmux(socket, "select-pane", "-t", pane)


def thread_read(thread_id):
    process = subprocess.Popen(
        ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        env={key: value for key, value in os.environ.items() if key not in ("TYPESAFE_API_KEY", "ELEVENLABS_API_KEY")},
    )
    pending = b""

    def call(number, method, params):
        nonlocal pending
        message = {"id": number, "method": method, "params": params}
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if b"\n" not in pending:
                if not select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                    break
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Codex history connection closed")
                pending += chunk
                continue
            line, pending = pending.split(b"\n", 1)
            response = json.loads(line)
            if response.get("id") == number:
                if "error" in response:
                    raise RuntimeError(response["error"]["message"])
                return response["result"]
        raise TimeoutError(f"Codex {method} timed out")

    try:
        call(1, "initialize", {"clientInfo": {"name": "jev_playground", "version": "0.1"}})
        process.stdin.write(b'{"method":"initialized"}\n')
        process.stdin.flush()
        try:
            return call(2, "thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]
        except RuntimeError as error:
            # Fresh interactive threads have no persisted history until their first prompt.
            if str(error) == f"thread not loaded: {thread_id}":
                return {"id": thread_id, "turns": [], "history_missing": True}
            raise
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()


def latest_reply(thread):
    for turn in reversed(thread["turns"]):
        if turn["status"] != "completed":
            continue
        for item in reversed(turn["items"]):
            if item["type"] == "agentMessage" and item.get("phase") in (None, "final_answer"):
                return item["text"]
    return "No completed reply yet."


def check_target(socket, agent):
    window, pane = agent["window_id"], agent["pane_id"]
    actual = tmux(socket, "display-message", "-p", "-t", pane,
                  "#{window_id}\t#{pane_current_command}")
    if agent["socket"] != socket or actual != f"{window}\tcodex":
        raise RuntimeError("The registered Codex pane is no longer running at this target")
    pid, title = tmux(socket, "display-message", "-p", "-t", pane, "#{pane_pid}\t#{pane_title}").split('\t', 1)
    identity = process_identity(pid, title=title)
    if identity['thread_id'] != agent['thread_id'] or not identity['thread_id']:
        raise RuntimeError("The pane's conversation changed or cannot be identified; try again")
    if agent.get('process') and identity['process'] != agent['process']:
        raise RuntimeError("The pane's process changed; try again")


def working_indicator(screen):
    return bool(re.search(r"(?m)^\s*[•·◦●].*\([^()\n]*esc to interrupt\)(?:\s*·[^\n]*)?\s*$", screen, re.IGNORECASE))


def interrupt_turn(socket, agent):
    check_target(socket, agent)
    if not agent.get('preserve_focus'):
        focus(socket, agent["window_id"], agent["pane_id"], agent.get("view_session", "tabs"))
    pane = agent["pane_id"]
    screen = tmux(socket, "capture-pane", "-p", "-t", pane)
    if not working_indicator(screen):
        return "No running turn is shown; nothing was interrupted."
    tmux(socket, "send-keys", "-t", pane, "Escape")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        time.sleep(.1)
        check_target(socket, agent)
        if not working_indicator(tmux(socket, "capture-pane", "-p", "-t", pane)):
            return "Interrupted the current turn. The session remains open."
    raise TimeoutError("Interrupt sent, but the session still shows a running turn")


def send_message(socket, agent, text):
    check_target(socket, agent)
    window, pane = agent["window_id"], agent["pane_id"]
    if not agent.get('preserve_focus'):
        focus(socket, window, pane, agent.get("view_session", "tabs"))
    tmux(socket, "load-buffer", "-b", "jev-message", "-", input=text)
    tmux(socket, "paste-buffer", "-b", "jev-message", "-d", "-p", "-t", pane)
    # Codex needs the bracketed paste processed before Enter submits it.
    time.sleep(0.2)
    check_target(socket, agent)
    tmux(socket, "send-keys", "-t", pane, "Enter")


def read_reply(socket, agent):
    check_target(socket, agent)
    if not agent.get('preserve_focus'):
        focus(socket, agent["window_id"], agent["pane_id"], agent.get("view_session", "tabs"))
    reply = latest_reply(thread_read(agent["thread_id"]))
    check_target(socket, agent)
    return reply
