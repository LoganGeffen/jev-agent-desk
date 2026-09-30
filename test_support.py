import time

from codex_actions import thread_read, latest_reply


def wait_reply(agent, expected, previous_turns=()):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        history = thread_read(agent["thread_id"])
        new_history = {"turns": [turn for turn in history["turns"] if turn["id"] not in previous_turns]}
        if latest_reply(new_history) == expected:
            return history
        time.sleep(.5)
    raise TimeoutError(f"Expected native reply: {expected}")
