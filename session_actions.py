import claude_actions
import codex_actions


def adapter(agent):
    provider = agent.get('provider', 'codex')
    if provider == 'claude':
        return claude_actions
    if provider == 'codex':
        return codex_actions
    raise ValueError('Unsupported agent provider')


def send_message(socket, agent, text):
    return adapter(agent).send_message(socket, agent, text)


def read_reply(socket, agent):
    return adapter(agent).read_reply(socket, agent)


def interrupt_turn(socket, agent):
    return adapter(agent).interrupt_turn(socket, agent)
