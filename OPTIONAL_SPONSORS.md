# ProofRun: selected additions and deferred sponsors

Status: **NEO4J VERIFIED ON AURADB FREE WITH LOCAL DOCKER; SIMILARWEB + BAND IMPLEMENTED LOCALLY.** See each integration's validation record for measured access and remaining gates.

On September 29, 2026, the user explicitly selected **Similarweb + BAND**, then requested **Neo4j** implementation. These requests supersede the earlier core-first deferral and one-at-a-time restriction for the selected additions. Plaud and Vultr remain deferred. Follow `CLAUDE.md` and `INTEGRATION.md` for the core build; its incomplete gates remain incomplete. Documentation, unit tests and saved reports do not establish a live sponsor integration.

Similarweb supplies dated website context for the FDE's meeting brief. BAND carries the exact repair candidate to the trusted verifier and returns the consequential verdict. Customer research cannot modify the test contract or repair status. Enabling BAND requires a completed room handoff; transport failure cannot fall back to direct verification.

## Core architecture and completion gate

The core is DuploCloud + Crusoe + OpenRouter:

- **DuploCloud:** starts a verification job and displays its measured result in an extension.
- **Crusoe:** a CPU VM hosts the verification worker and isolated test execution.
- **OpenRouter:** the model gateway supplies an actual bounded repair proposal.

Crusoe's required role in this architecture is worker hosting. Routing inference to Crusoe through OpenRouter is not required by this plan. The first supported behavior check is a bounded Python/Pydantic case with trusted acceptance tests; it is not arbitrary repository repair.

Person 1 assembles the completion record; Person 2 supplies verification evidence; Person 3 supplies worker and model evidence. These gates still determine core completion. The user's selected additions can be implemented alongside this work.

- [ ] A real job starts in DuploCloud, reaches the worker, and returns its result to the extension UI.
- [ ] The job executes on the Crusoe CPU VM. Record worker identity and execution artifacts; local execution and replayed results do not satisfy this check.
- [ ] OpenRouter performs the repair-proposal call. Record model identity and redacted operation evidence; a prepared response does not count.
- [ ] The approved behavior passes in the baseline and fails under the supported update. Setup failures, missing tests, timeouts, and unavailable workers remain distinct outcomes.
- [ ] A candidate repair passes the original trusted tests and independent controls. A deliberately overpermissive repair is rejected. The proposer cannot weaken the test contract to obtain a pass.
- [ ] Evidence identifies source revision, dependency environment, case/contract hash, candidate hash, and execution results. Changed inputs invalidate previous acceptance evidence.
- [ ] Another teammate completes a fresh run from the documented setup. The integrated demo revision, actual sponsor contributions, simulated inputs, and remaining limitations are recorded.

| Completion record | Value |
|---|---|
| Integrated revision | Not recorded |
| Core run ID / evidence location | Not recorded |
| Trusted-test and rejected-bad-fix results | Not recorded |
| DuploCloud / Crusoe / OpenRouter evidence | Not recorded |
| Integration owner | Person 1 |
| Gate status | DEFERRED |

After every core check passes, record `CORE_COMPLETE` with evidence. The selected scope is Similarweb, BAND and Neo4j. Plaud and Vultr are outside this increment.

## Optional integrations

The sections below describe responsibilities. Consult [Similarweb setup](docs/SIMILARWEB.md), [BAND setup](docs/BAND.md), and [Neo4j setup](docs/NEO4J.md) for implementation, configuration and validation. Live access must be recorded separately from implementation checks. Do not expose credentials in chat, logs, reports, frontend code, or test containers.

### Neo4j — choose which deployments need rechecking

- **Owner:** Person 2; Person 1 integrates the resulting selection view.
- **Selected scope:** explicit deployment and contract relationships; activated by the user's September 29, 2026 implementation request.
- **Input / mechanism:** store explicit deployment, revision, contract, case, and measured-run relationships using the Python driver and parameterized Cypher. A contract change returns affected deployment IDs and explaining paths.
- **Output:** jobs marked `requires_reverification`. Graph traversal selects work; executed tests determine behavior.
- **Access:** an available Neo4j database, URI, and credentials. Do not assume an Aura account or instance exists.
- **Acceptance:** three synthetic deployments share two contracts; changing one selects exactly the two dependent deployments. Run a selected job through the core engine and prevent reuse of evidence for the old contract.
- **Boundary:** registration is explicit; there is no automatic infrastructure discovery or deployment trigger. Neo4j stays off until configured, fails visibly when unavailable, and never substitutes memory storage. The registered core fixture must match the selected source and target contract before dispatch. See [the setup and acceptance guide](docs/NEO4J.md).

[Python driver](https://neo4j.com/docs/python-manual/current/query-simple/) · [Aura Free](https://neo4j.com/free-graph-database/)

### Plaud — propose a requirement from a recorded conversation

- **Owner:** Person 1; Person 2 maps an approved case into the verifier's contract.
- **Input / mechanism:** retrieve an available, consented recording's transcript through Plaud MCP; extract a proposed requirement with its source reference. An engineer confirms the expected behavior before it becomes a test contract.
- **Output:** an approved structured case plus transcript provenance. Ambiguous statements remain proposals.
- **Access:** Plaud account authentication, compatible runtime, and a usable recording. The MCP connector is not an arbitrary-audio transcription service; do not assume a device or recording is available.
- **Acceptance:** retrieve a synthetic conversation stating that nickname is optional, obtain explicit confirmation of that behavior, and execute the resulting case through ProofRun.
- **Defer if:** there is no usable transcript or the integration only generates a meeting summary.

[Plaud MCP documentation](https://docs.plaud.ai/plaud-mcp-cli/mcp)

### Vultr — prove worker portability or controlled recovery

- **Owner:** Person 3; Person 2 supplies the unchanged worker conformance cases.
- **Use only when:** a second execution host solves a real portability or availability need. Retain a demonstrated Crusoe execution path.
- **Input / mechanism:** send the same versioned job to a compatible worker on Vultr; preserve execution limits and evidence semantics. A remote VM is not automatically a sandbox. Evaluate any alternative sandbox's isolation and network controls separately.
- **Output:** the same result schema, including actual host, environment, job, and attempt identities.
- **Access:** suitable funded or credited compute, deployment access, and runtime prerequisites. Do not assume cloud credits or virtualization support.
- **Acceptance:** demonstrate successful Crusoe execution, a controlled worker-unavailable condition, and an explicitly identified retry on Vultr. Both hosts pass the same conformance cases; worker failure is not an application failure, and retries cannot duplicate publication actions.
- **Defer if:** an additional host contributes no demonstrated benefit beyond sponsor count.

[Vultr agent-sandboxing guide](https://docs.vultr.com/how-to-set-up-agent-sandboxing-on-vultr-cloud-compute)

### BAND — make the proposer-to-verifier handoff consequential

- **Owner:** Person 1 coordinates the integration; Person 3 owns the proposer participant and Person 2 owns the verifier participant.
- **Input / mechanism:** separately registered agents exchange the candidate revision, contract hash, and evidence through a BAND room. The verifier receives the handoff, executes trusted checks, and returns a consequential `BLOCKED` or `PASS` result. Keep room execution events visible.
- **Output:** a review result tied to the exact tested candidate. Application code enforces the block; model prose alone cannot approve a repair.
- **Access:** BAND account, separate agent IDs/API keys, SDK runtime, model access, and running agent processes. The SDK receives WebSocket messages; MCP-only command posting is not equivalent.
- **Acceptance:** a bad candidate is blocked through the room, a corrected candidate is freshly checked, and disconnecting BAND prevents the dependent handoff. No direct bypass should let Duplo sequence both agents while BAND merely mirrors their log.
- **Boundary:** Duplo starts/displays the case; BAND coordinates these participants. Adding BAND must not duplicate orchestration without changing behavior.
- **Defer if:** there is no useful independent participant or the room is only status logging. This option is not required for the three-sponsor core.

[BAND hacker guide](https://www.band.ai/hacker-guide) · [SDK documentation](https://docs.band.ai/integrations/sdks/overview)

### Similarweb — customer research in the meeting brief

- **Owner:** integration work selected by the user on September 29, 2026.
- **Use:** prepare questions about the customer's operating scale before the meeting. Website traffic provides context; actual import/API volume must be confirmed with the customer.
- **Mechanism:** the FDE supplies a domain and completed month and explicitly requests research. A server-side Similarweb call returns estimated worldwide website visits with period, source and retrieval time. The result appears in customer context and the downloadable meeting brief.
- **Boundary:** research has separate records and request identities. It does not change a source snapshot, approved requirement, executed check, or repair verdict. Repeating the same request identity reuses its stored result rather than spending credits again.
- **Acceptance:** a real authorized API response for the chosen customer domain appears in the brief with its date/provenance; missing access and missing data remain visible. Tests with stubbed transport are local implementation checks only.

[Similarweb Websites Dataset](https://developers.similarweb.com/docs/websites-dataset) · [Similarweb MCP setup](https://docs.similarweb.com/api-v5/similarweb-mcp/mcp-setup)

The MCP setup page, reviewed September 29, 2026, lists `https://mcp.similarweb.com` as the server endpoint. It requires a Similarweb login, a compatible MCP client and a subscription with API/MCP access (API-only, Business or Enterprise). MCP queries consume data credits; entitlement and event access have not been verified for this project. This reference does not establish live Similarweb access.

## Three-person coordination and order

1. Preserve the core verification behavior and its honest completion record.
2. Implement the user-selected Similarweb, BAND and Neo4j additions; keep Plaud and Vultr deferred.
3. Agree its versioned input/output contract, exact file ownership, and smallest acceptance check.
4. Implement in the established branch/worktree arrangement; separate chats alone do not isolate edits.
5. Integrate through Person 1, rerun relevant core checks, and record actual sponsor contribution.

| Person | Optional responsibility | Required handoff |
|---|---|---|
| 1 | Gate record, Duplo integration, Plaud intake, BAND coordination, Similarweb assessment | Approved case/interface and integrated evidence |
| 2 | Trusted verification, Neo4j selection, BAND verifier | Affected IDs, contract mapping, executed checks |
| 3 | Repair/infrastructure, Vultr portability, BAND proposer | Candidate/worker identity, environment evidence, recovery results |

Each handoff names the revision, changed paths, interface version, dependencies, what actually ran, evidence location, mocks or synthetic inputs, unresolved blockers, and next owner. Agree interface changes before dependent edits; avoid concurrent changes to shared files. Do not commit unless explicitly requested.

Use the core ownership map in `INTEGRATION.md`. Preserve its `/v1/runs` API and `proofrun.v1` contract, including independent execution, finding, and repair states and revision/source/contract/candidate bindings. Optional work must not reinterpret a completed execution as a passing finding or accepted repair.

## Selected and deferred work record

| Sponsor | Initial status | Reason / activation evidence |
|---|---|---|
| Neo4j | AURA FREE LIVE CHECK PASSED | User-approved $0/hour AuraDB Free instance selected exactly two of three synthetic deployments; selected native Docker run executed 14 checks, retained four hash-checked artifacts and rejected old-contract reuse. See [Aura evidence](INTEGRATION.md#auradb-free-activation--september-29-2026). |
| Plaud | DEFERRED | Core incomplete; usable recording and intake need unverified |
| Vultr | DEFERRED | Core incomplete; portability/recovery need unverified |
| BAND | LOCAL CHECKS PASS | Consequential room handoff implemented; live credentials and room evidence pending |
| Similarweb | LOCAL CHECKS PASS | Customer context adapter and brief UI implemented; authorized API access/domain/evidence pending |

Update the record with evidence rather than assuming activation. A connected account, logo, unexecuted path, or historical report does not establish a meaningful integration. Technical success does not guarantee prize eligibility; verify current [event rules](https://hackersquad.io/events/cmq5jhvv400j4p20koptqeyoa) before making eligibility claims.
