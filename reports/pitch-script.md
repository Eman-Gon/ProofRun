# Hackday Idea: project walkthrough

Hackday Idea helps developers investigate what a dependency upgrade could change. It brings source checks, targeted comparisons, and saved results into one local workspace.

## Repository checks

Open the dashboard, choose a public repository, and run a check. Inspect each warning's source location, explanation, and suggested next step. These checks apply supported static rules; they do not execute the application. A result with no matching warnings is limited to the scanner's coverage.

## Prepared comparisons

Choose a saved comparison to inspect the dependency versions, targeted input, captured test output, and proposed fix. Run it again to collect fresh Docker results. A prepared comparison establishes behavior only for its source snapshot and supplied tests.

## Investigation history

Saved results let developers revisit the source context and outcomes behind a finding. A live investigation can retrieve upstream context and store confirmed results for later retrieval. Describe service operations only from the selected run's actual status and evidence.

## Current scope

Public repository inspection supports known patterns. Measured comparisons use prepared cases. Proposed patches are tested in disposable copies, and the tool does not apply them to the original repository. The next step after a static warning is a targeted test in the project's own environment.
