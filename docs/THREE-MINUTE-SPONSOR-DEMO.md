# Three-minute ProofRun demo

Rehearsed September 29, 2026. Speak the quoted paragraphs; the directions are not spoken. Allow time for clicks. This is a presentation script, not a video recording.

## 0:00–0:25 — Customer problem

Show localhost:8765 and select **Customer import demo**. Click **Run comparison**.

“Imagine a customer meeting in twenty minutes. We updated a dependency, and our existing tests are green. But does the customer workflow still work? ProofRun checks a specific promise: customers can import a record without supplying a nickname. This demonstration uses prepared synthetic examples and isolated local Docker environments.”

## 0:25–0:55 — Reproduce and repair

Wait for completion; point at the targeted-check row, then the suggested change.

“Here are the same checks before and after the Pydantic upgrade. The existing tests pass in both. Our missing-nickname example passes before the upgrade and fails afterward. The prepared fix adds an explicit None default. That fix passes in both environments. We keep the original failure visible, so a passing repair does not erase what broke.”

If the fresh run fails, show that failure and do not describe old results as fresh.

## 0:55–1:20 — Neo4j

Show the evidence graph and select **probe: after, fail**.

“Neo4j stores the relationships behind these results: the finding, proposed fix, environments, checks, and evidence artifacts. Selecting this failed check shows its connection to the regression. The graph makes the result inspectable. It does not approve a fix; the actual checks determine the outcome. This graph is backed by the configured Aura database.”

## 1:20–1:55 — OpenRouter and BAND

Show the recorded sponsor evidence summary below; label it a separate recorded run.

“Our generated-repair path uses OpenRouter for model access. BAND carries the exact proposed candidate to a separate verifier participant and returns its verdict. In this recorded live run, the candidate was generated through OpenRouter, the BAND round trip passed, and all fourteen repaired checks passed. The comparison we just clicked uses a prepared fix; this recorded run demonstrates the generated path.”

Recorded evidence: `../.commit-watch/band-fix-20260929/live-openrouter/receipt.json`. Run `run-7fdecbb652404432aead0d43e4daa897`; execution completed, regression reproduced, repair verified, BAND live/passed, 14 repaired checks passed, 11 hash-checked artifacts. Do not display private environment files.

## 1:55–2:25 — DuploCloud and Crusoe

“DuploCloud provides the deployed extension for starting jobs and displaying evidence. Its gateway has been exercised; the browser presentation still needs its own sign-in and walkthrough. Crusoe is the intended CPU worker host, while today's comparison runs locally. The engineer receives the result and its supporting artifacts together, so they can inspect the evidence before deciding what to change.”

## 2:25–3:00 — Public repository and PR handoff

Select **Public repository**, enter `loganngarcia/proofrun-demo-app`, and click **Check repository**. Show the suggested change at `app.py:9`. Point to **Create draft PR** below the change and above the graph. Opening its confirmation is sufficient for rehearsal; cancel rather than publishing a rehearsal PR.

“For a public repository, ProofRun can also inspect supported migration patterns and prepare a draft PR. This scan proposes adding the missing default. The button is directly beneath the change. These repository suggestions are explicitly unverified because this scan does not run repository tests. The engineer reviews the patch and runs the relevant tests before merging. ProofRun's handoff is a specific change with a clear account of what was actually checked.”

## Presenter evidence boundaries

- Fresh rehearsal: local prepared Docker comparison and interactive Neo4j graph.
- Recorded live evidence: OpenRouter generation and BAND delivery/verdict.
- DuploCloud: deployed extension and gateway evidence; do not substitute localhost:8765 for the portal.
- Crusoe hosting: do not claim demonstrated without new provider evidence.
- Public scan: source review and unverified draft proposal, not arbitrary repository execution.
