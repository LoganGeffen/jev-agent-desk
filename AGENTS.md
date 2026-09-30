# Working on Jev Agent Desk

- For setup and configuration, use [README.md](README.md).
- Before changing request routing, agent control, or voice behavior, read
  [ARCHITECTURE.md](ARCHITECTURE.md) for the data flow and module boundaries.
- Keep changes scoped to the requested behavior. Add dependencies or abstractions
  only when a concrete requirement needs them.

Preserve these behaviors:

- Literal messages retain their exact wording and captured recipient identity.
  Changed or unresolved identities block delivery.
- Uncertain delivery never retries automatically; submitted input is not task completion.
- Original replies remain available alongside optional spoken renditions.
- Stopping readback is independent of interrupting the agent; late results cannot restart audio.

Use synthetic providers and disposable tmux sessions for tests. Existing agent
conversations require explicit authorization for live interaction. Keep credentials
and `.run/` data out of commits, and keep control services bound to loopback.

After code changes, run the relevant tests; the complete suite is
`python check.py` from the configured virtual environment. Follow the README's
test setup for its dependencies. Report what was exercised and any remaining gaps.
