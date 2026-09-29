# BAND proposer and verifier handoff

The optional BAND path is off by default in example configuration. On September
29, 2026, a fresh OpenRouter-generated repair completed a live BAND round trip,
passed all 14 native Docker repair checks, and produced 11 hash-checked artifacts.
The main local `.env` and `.env.integrations` now enable BAND. Worker processes
must load those settings at startup; editing the files does not update an
already running worker. Credentials and an existing room are still required.

Evidence: `.commit-watch/band-fix-20260929/live-openrouter/receipt.json` and
`run.json`, run `run-7fdecbb652404432aead0d43e4daa897`. This checks the registered
synthetic fixture with local Docker, not a browser-initiated or deployed-worker
round trip. Live blocked-candidate and disconnect scenarios remain separate
acceptance checks; those failure paths are covered by offline tests.

## What runs

After the native worker reproduces the regression and OpenRouter proposes a
candidate, two separately registered BAND identities connect from that worker.
The proposer sends the candidate, source revision, contract and acceptance
hashes into the configured room. The verifier participant must receive that
exact message over the SDK WebSocket before the trusted Docker verifier runs.
It posts measured case outcomes and `PASS` or `BLOCKED` back into the room. The
proposer must receive that exact result before the service can accept the repair.

The worker still checks its original evidence contract after the room round trip.
An unavailable room, missing delivery, changed candidate, unexpected sender,
stale message, lost connection, changed environment or incomplete verification
cannot approve a repair. The already reproduced regression remains recorded.
A rejected first candidate can use the existing bounded second proposal attempt;
that attempt needs a new room exchange and fresh verification.

This is a fixed workflow with two co-hosted SDK participants, not a second model
judge. BAND transports the handoff; tests determine the verdict. The participants
share the trusted worker process and are not separate operating-system security
boundaries. The repair candidate still runs only in the existing isolated Docker
containers. This implementation does not reply to human room messages, invite
participants or create rooms automatically.

## Private setup

1. In [BAND](https://app.band.ai), register two Remote Agents, such as
   `ProofRun Proposer` and `ProofRun Verifier`. Save each agent's UUID and its
   separate API key privately.
2. Create a dedicated room containing those two agents and record its UUID.
   Avoid using these same agent identities in another running SDK session:
   BAND can supersede the old connection, which correctly blocks this attempt.
3. Install the optional transport into the same Python 3.12 environment as the
   worker:

   ```bash
   python -m pip install -r requirements-worker.txt -r requirements-band.txt
   ```

4. Export these settings privately before launching the API. The worker does
   not automatically load environment files. Preserve the existing worker and
   OpenRouter configuration.

   | Variable | Value |
   | --- | --- |
   | `PROOFRUN_BAND_ENABLED` | `true` to require the room handoff |
   | `BAND_ROOM_ID` | Existing room UUID |
   | `BAND_PROPOSER_AGENT_ID` | Proposer Remote Agent UUID |
   | `BAND_PROPOSER_API_KEY` | Proposer's private agent key |
   | `BAND_VERIFIER_AGENT_ID` | Different verifier Remote Agent UUID |
   | `BAND_VERIFIER_API_KEY` | Verifier's separate private agent key |
   | `PROOFRUN_BAND_TIMEOUT_SECONDS` | Optional delivery/acceptance deadline, 5–900 seconds; default 240 |

   ```bash
   python -m src.proofrun.api --host 127.0.0.1 --port 8766 --runner native
   ```

5. Start a fresh registered verification with repair enabled from the extension
   or authenticated worker collector. The worker starts and closes both BAND
   sessions for each candidate; a separate long-running agent command is not
   needed. There is no prepared-fix fallback if the model is unavailable.

Only the official cloud endpoints `https://app.band.ai` and
`wss://app.band.ai/api/v1/socket/websocket` are supported in this slice. IDs, keys
and room membership are checked before dispatch. Credentials are never included
in room messages or browser results. Candidate application bytes are sent to
this room; use the registered synthetic fixture and a dedicated room.

The configured deadline prevents acceptance after delivery or verification
expires. It is not a hard cancellation timer for a Docker check already running
in a Python thread. That check remains bounded by the native runner's own limits,
and cleanup may wait for it to finish. It cannot produce an accepted repair
after the handoff deadline.

## Evidence and failure behavior

Run results include a `coordination` object with provider, live/mock mode, status,
room/agent/message identities, candidate and contract hashes, and the measured
evidence digest. Each completed attempt publishes a hash-checked
`attempt-N-band-handoff` artifact and keeps its own coordination receipt.
`waiting` means delivery or verification is in progress; `passed` and `blocked`
describe the handoff's checked result; `unavailable` means it could not complete.
Execution, reproduced finding and repair status remain separate.

The live verification screen shows observed handoff stages: sending the candidate,
verifier receipt, Docker verification, and return delivery. Progress is saved with
the run and appears on the next poll; very short stages may pass between polls.
An expandable receipt shows the room, agents, message IDs and evidence hashes.
Failures preserve the last observed stage and never display a completed handoff.

The room receives candidate and verdict messages plus task events when
verification starts and finishes. Room messages and provider responses are data,
not instructions: their sender, room, message ID, one-use correlation ID and
exact bounded JSON contents must match. Worker-local artifact paths, raw logs,
model rationale and secrets are not sent. SDK logs that may contain private
headers or WebSocket query strings are suppressed while the sessions are open;
public failures use a fixed safe message.

BAND renders a leading `@[[recipient-agent-id]] ` into delivered message text.
After checking routing metadata, the SDK adapter removes only the exact prefix
for the receiving identity. Plain JSON delivery is also supported. The strict
decoder still rejects other prefixes, trailing text, duplicate JSON keys and
non-finite numbers, and the decoded candidate or result must match the expected
payload. The wire-size limit is checked before removing the mention.

## Checks and live acceptance

```bash
python -m pytest tests/test_proofrun_band.py tests/test_proofrun_band_sdk.py tests/test_proofrun_service.py -q
```

Tests use mock room transport and mock SDK REST/WebSocket delivery; network is
blocked. SDK tests import the installed pinned `band-sdk==3.2.1` and its actual
request/payload types. They are skipped if the optional SDK is absent. Install
`requirements-band.txt` when validating the BAND boundary.

For live acceptance, retain the room messages and worker artifacts from a bad
candidate receiving `BLOCKED`, a corrected candidate receiving `PASS`, and a
disconnected or inaccessible room producing repair `unavailable`. Use the
existing trusted verifier; do not weaken it to obtain a pass. Local execution
does not establish Crusoe hosting or a DuploCloud-initiated round trip.

Implementation references, inspected September 29, 2026:
[SDK overview](https://docs.band.ai/integrations/sdks/overview),
[setup](https://docs.band.ai/integrations/sdks/tutorials/setup), and the
[official Python SDK](https://github.com/band-ai/band-sdk-python). This adapter
uses the SDK's documented `BandLink` event iterator and REST request models.
The pinned SDK's transient-disconnect callback is overridden to latch failure:
its ordinary connection flag alone stays true during reconnect and cannot prove
an uninterrupted handoff.
