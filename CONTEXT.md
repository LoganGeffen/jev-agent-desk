# Jev Agent Desk

## Language

**Working**: The current conversation has a native turn start with no later end. Process existence or recent terminal output alone does not establish this state.

**Reply ready**: The current turn ended with a final reply. The reply can be read; this does not establish that the user's overall task is complete.
_Avoid_: Done, task complete

**Idle**: Verified native evidence says there is no active turn and no current final reply.

**Stopped**: The current turn ended through interruption. This is not a successful reply.

**Needs you**: A verified request for required user input or approval. An error or silence alone does not establish this state.

**Status unknown**: Current native lifecycle evidence is missing, stale, unavailable, or contradictory. Last known replies remain readable separately.

**Reply notification**: A one-time notice of a newly observed current final reply, corroborated by native lifecycle evidence. Startup history, replaced conversations, commentary, and interrupted turns do not generate reply notifications.
