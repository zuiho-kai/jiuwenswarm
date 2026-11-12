# Team message routing patch

This branch packages the implementation as an agent-core dependency patch.
JiuwenSwarm pins agent-core to commit
`2f4bedb9c5d85ab70d7c79d36183e6a98799e94e`; the application dependency
declaration is unchanged. Checking out this branch alone does not activate it.

## Apply

Clone agent-core, check out the pinned commit, then run from that checkout:

```sh
git apply --check /path/to/jiuwenswarm/patches/agent-core-private-message.patch
git apply /path/to/jiuwenswarm/patches/agent-core-private-message.patch
```

From a JiuwenSwarm checkout with dependencies already installed:

```sh
uv pip install --no-deps -e /path/to/agent-core
uv run --no-sync jiuwenswarm-start
```

Do not resync the original pinned dependency over the editable installation.
Keep API credentials outside the repository.

## Behavior

- Private messages accept steer, follow_up and leader-only abort.
- Ordinary team broadcasts use receiver-side Jev: steer, follow_up or drop.
- Public group mentions bypass Jev when idle. Busy recipients use only steer
  or follow_up; errors fall back to follow_up. Unmentioned members are not woken.
- Group follow-ups use the existing runtime queue. A steer does not clear it.
- Broadcast submission is not acknowledgment. Leader summaries distinguish
  confirmed receipt from unconfirmed receipt.
- JEV_ENDPOINT_PATH supports providers using /v1/decisions; the default remains
  /v1/systemone.

## Validation

Targeted group conversation, group chat integration, message handler and Jev
tests: 79 passed. Earlier real service team rehearsals using Mindshub Jev
observed steer, follow_up and drop for the same broadcast. Public group mention
routing has automated coverage; it has not had a live group chat rehearsal.
