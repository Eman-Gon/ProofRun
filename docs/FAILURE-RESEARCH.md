# Research a recorded failure

**Research this failure** connects a measured break to related GitHub issues,
official documentation and release notes. It is available for a completed
fixture run with a reproduced regression and for each confirmed finding in a
completed release investigation. Similarweb customer context remains separate.

## Use the workflow

1. Open a recorded failure and select **Research this failure**.
2. Review the search query. Include the public package name, affected versions
   and relevant error. The prepared Pydantic case can offer a query when its
   recorded observations match; generic release findings do not invent
   dependency versions. Their search field starts empty.
3. Submit the query. The worker uses OpenRouter web search and returns an
   advisory summary, dated source links/excerpts and suggested fixes.
4. Inspect the sources and affected versions. Download the research brief if
   useful for the meeting or code review.
5. Select **Try a repair with this research** to create a fresh repair run.
   The proposal receives bounded research as untrusted advice. The normal
   protected tests and verifier decide whether the candidate passes.

The original finding, requirements and verdict stay intact. Research does not
establish causation, confirm that another issue matches, or mark a fix verified.
The action does not commit or deploy changes. A generic release target must
already permit repair and still match the selected revisions and contract.

## Configuration and requests

Use the worker's existing private `OPENROUTER_API_KEY` and explicit
`PROOFRUN_MODEL`. No Similarweb key is used. The implementation uses
[OpenRouter's web search server tool](https://openrouter.ai/docs/guides/features/server-tools/web-search)
with Exa, a bounded result count and a 60-second request deadline. Search and
model usage can consume OpenRouter credits. No provider call happens simply
because a worker starts, a result is refreshed, or a finding is displayed.

Only the query shown in the research form is sent to the research provider.
The worker keeps the finding evidence, source/contract hashes and workspace
binding locally. Do not include private customer data or secrets in the query.
Source excerpts and suggestions are displayed as text, with validated HTTPS
links. Browser clients never receive worker or provider credentials.

The fixture worker accepts authenticated
`POST /v1/runs/{run_id}/failure-research` with `request_id` and `query`.
The release worker accepts authenticated
`POST /v1/release-runs/{run_id}/failure-research` with `request_id`, `query`, and
`finding_id`, under `X-ProofRun-Scope`. The DuploCloud gateway derives the scope
from the authorized workspace and checks resource ownership for fixture runs.

Reports use `proofrun.failure-research.v1` with statuses:

- `completed`: usable retrieved citations support the returned advice.
- `no_sources`: no useful cited result; no repair suggestion is inferred.
- `unavailable`: missing configuration, provider failure, timeout, interrupted
  request or invalid response. No successful search is claimed.

Each request has a durable claim before provider access. Retrying the same
request reuses the recorded result; changed inputs with the same identity are
rejected. An explicit new search gets a new identity and may consume credits.
Source links must come from provider citation annotations, and each suggestion
must reference those retrieved sources. Model-invented links are rejected.

Selecting research for a fresh fixture repair adds `repair.research_id`; for a
fresh release investigation it adds `failure_research_id` with `repair: true`.
The worker rejects unsourced, unavailable, stale or differently bound reports
before starting a repair. Reports are saved in the worker's separate
`failure-research` artifact directory. The UI holds its current research display
for the page session; keep an exported brief for later review.

## Validation

```bash
python3.12 -m pytest tests/test_failure_research.py tests/test_failure_research_api.py \
  tests/test_release_research_handoff.py tests/test_proofrun_service.py \
  tests/test_proofrun_repair.py -q
```

Frontend helper checks include `failure-research.test.mjs`; the C# gateway
checks are in `extensions/proofrun/failure-research-tests`. Those checks use
injected transport and synthetic reports. They do not establish a live search,
portal deployment or verified repair. Record live evidence separately.

## Measured local workflow — September 29, 2026

A fresh native fixture check reproduced the Pydantic regression, followed by a
live OpenRouter/Exa lookup returning five cited sources and three suggestions.
A fresh run consumed that stored research, received an actual generated repair,
and passed all 14 original-suite/control checks. The original failed finding
remained unchanged. All ten repair-run artifacts were checked against their
recorded sizes and SHA-256 hashes.

- Original run: `run-2899f3f403d1419e90200588513e271c`.
- Repair run: `run-cc94781b55af44c4817c7b36cffeed86`.
- Receipt: `.commit-watch/failure-research/workflow-4b2b2300/receipt.json`.
- Implementation checks and source hashes: `.commit-watch/failure-research/validation.json`.

This establishes the local synthetic fixture, live research and live generated
repair handoff. It does not establish a portal button-to-worker round trip for
this addition, Crusoe execution, or a research-backed generic release run.
Generic release handoff and browser behavior have separate injected-provider
tests. Earlier failed search attempts are retained in the same evidence area;
they prompted the tested structured-output/fence handling and 60-second deadline.
