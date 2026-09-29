# FDE demo: a customer meeting in 20 minutes

An FDE is about to demonstrate a customer-import workflow after a dependency update. The customer permits an omitted nickname, but malformed nicknames and missing names must still be rejected. The FDE manually chooses **Check this release** to check five approved, synthetic examples before the meeting.

This is a fictional customer scenario backed by a runnable, registered fixture: `customer-nickname-v1`. The application and cases are prepared. ProofRun compares `import_row` under Pydantic **1.10.18** and **2.8.2** in local Docker, then can independently verify a bounded candidate. The fixture invokes a Python function directly; it does not start a customer API, replay staging traffic, observe a deployment event or discover arbitrary bugs.

## Approved examples

The source of expectations is [contract.json](../demo/upgrade/contract.json), not a repair proposal.

| Example | Synthetic input | Required behavior |
|---|---|---|
| Nickname omitted | `{"name":"Grace"}` | Accept; return `nickname: null` |
| Explicit null | `{"name":"Grace","nickname":null}` | Accept; return `nickname: null` |
| String nickname | `{"name":"Grace","nickname":"  Amazing Grace  "}` | Accept; preserve the string, including spaces |
| Object nickname | `{"name":"Grace","nickname":{"unexpected":"object"}}` | Reject with `ValidationError` |
| Required name missing | `{"nickname":"Grace"}` | Reject with `ValidationError` |

Each environment also runs the two [original tests](../demo/upgrade/test_existing.py). These cover an explicit nickname and explicit null; they miss omission. Seven checks run per environment.

## Run the local proof

Complete [local setup](../README.md#local-setup), including image preparation, before presenting. Keep the existing private worker settings and tokens; never paste them into the demo or its artifacts.

First, execute the comparison and prepared repair controls from the repository root in the worker virtual environment:

```bash
source .venv/bin/activate
python -m demo.upgrade.verify_offline
```

The command prints the new `experiment.json` path under `.commit-watch/proofrun-verifier/`. It returns `0` only when all three expected outcomes below are measured; otherwise it returns `2`. Use that run's evidence, or explicitly label an older artifact as a recorded run.

| Application tested | Pydantic 1.10.18 | Pydantic 2.8.2 | Expected verdict |
|---|---|---|---|
| Original `Optional[str]` | 7/7 pass | 6/7 pass; omission fails | Regression reproduced |
| Prepared narrow fix: `Optional[str] = None` | 7/7 pass | 7/7 pass | Verified against the declared checks |
| Prepared permissive fix: `Any = None` | 6/7 pass | 6/7 pass | Rejected: invalid object accepted |

These are expected fixture outcomes, not a receipt for a run that has not happened. The permissive candidate fixes omission and passes the original suite, but fails the independent object-rejection control in both environments. Both candidates are checked-in examples; this command does not call a model.

For the authenticated worker path, start the native worker using the [README commands](../README.md#prepare-and-run-the-worker). In another terminal with the same private environment exported, submit a fresh comparison and download hash-checked artifacts:

```bash
python deploy/crusoe/run-worker.py --expected-target local \
  --output-dir .commit-watch/fde-demo-fresh-run
```

Choose a new output directory for every run. Inspect `collection.json`, `record.json` and the collected artifacts. A successful comparison should report execution `completed`, finding `regression_reproduced` and repair `not_requested`. The collector's HTTP traffic talks to the ProofRun worker, not a customer staging API. The separate offline experiment above demonstrates prepared repair acceptance and rejection.

The extension's manual action is **Check this release**. Use it only after the [actual DuploCloud extension](../extensions/proofrun/README.md) is deployed and connected; retain its real resource/run IDs and evidence. Until that setup works, present the CLI/worker execution as local proof. Enabling generated repair requires an actual configured provider; an unavailable provider preserves the finding and reports repair unavailable.

## Two-minute talk track

Use the following after inspecting a fresh successful run; replace result claims with the actual outcome if a check fails.

**0:00–0:25 — The customer promise.** “I am an FDE with a customer meeting in 20 minutes. Our import workflow permits a missing nickname. I have five approved synthetic customer examples, including invalid inputs that must stay invalid. I want to know whether this dependency update preserves that agreement.”

**0:25–0:55 — The comparison.** “ProofRun runs the same application and checks in two pinned environments. The original suite passes in both, but the additional omitted-nickname example succeeds on the baseline and fails after the update. This local demo calls the import function in Docker. It is not sending requests to a customer service.”

**0:55–1:25 — The misleading fix.** “Here are two prepared candidates. Adding an explicit null default preserves the string constraint and passes all seven checks in both versions. Changing the field to accept anything also removes the original failure, but now an object-valued nickname gets through. The independent control rejects that candidate.”

**1:25–2:00 — What the engineer receives.** “The result separates execution, the reproduced finding and the repair verdict. It retains the failing example, candidate and evidence tied to the source, checks and environment actually tested. I can review this narrow change before the meeting. Today’s proof is local, synthetic and uses prepared candidates. The planned integrated flow adds DuploCloud initiation, a Crusoe-hosted worker and an OpenRouter-generated proposal, with the same verifier deciding whether the proposal passes.”

## What each demonstration establishes

| Demonstration | Available proof or remaining gate |
|---|---|
| Local comparison and prepared controls | Runnable Docker experiment; inspect each fresh artifact before claiming its result |
| Worker submission and artifact delivery | Authenticated local HTTP collector; separate from portal initiation |
| DuploCloud portal | Extension source exists; actual deployed initiation and display still need evidence |
| Crusoe hosting | Deployment code exists; local Docker is not evidence of execution on a Crusoe VM |
| OpenRouter repair | Proposal integration exists; a live provider-generated candidate and its verification still need evidence |

The user selected Similarweb customer context and the BAND proposer/verifier
handoff for implementation alongside the core. Only show either as live when
its actual provider evidence exists; see [Similarweb setup](SIMILARWEB.md) and
[BAND setup](BAND.md). The other optional sponsors remain deferred.

For customer research, select the customer's public website and a completed
month, then explicitly request Similarweb context. The meeting brief can include
estimated website visits with retrieval time and source. Use this to prepare
questions about operating scale; website visits do not measure import/API volume
and cannot change the release or repair verdict.

For BAND, enable the configured repair handoff and request generated repair.
The exact candidate must arrive at the verifier through the room before trusted
checks execute; the verdict must return before acceptance. Room disconnection
must leave the repair unavailable. A prepared or mock room test must retain its
label and cannot be presented as live BAND execution.

Use [INTEGRATION.md](../INTEGRATION.md) for the current integration record. A passing candidate establishes only the declared behaviors in these two environments; it does not certify the whole product or perform a deployment.

## Meeting summary checks

The extension leads with a plain-English assessment and offers **Download meeting brief** for a selected recorded run. A verified candidate leaves the original update marked blocked. Incomplete checks cannot become a passing assessment, and the brief includes observation time, execution target, proposal provenance and evidence bindings. Staging remains unverified by this fixture.

Run the presentation logic checks with Node 22.11 or later, without the portal or Docker:

```bash
cd extensions/proofrun/frontend
node --experimental-strip-types --test tests/release-summary.test.mjs
```

These tests exercise status interpretation and brief export. They do not execute customer examples or establish any sponsor integration.
