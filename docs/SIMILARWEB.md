# Similarweb customer research

Similarweb supplies dated customer context for a meeting brief. It is separate from ProofRun's test evidence: traffic estimates cannot change case expectations, repair instructions, execution status or a verification verdict.

The implemented first slice looks up one website and one completed calendar month. It requests estimated total visits for worldwide desktop and mobile web traffic, including subdomains. The provider contract is the official [Visits — Total API](https://developers.similarweb.com/reference/visits), inspected September 29, 2026. That reference documents subscription-dependent history and a price of one hit per request or one data credit per result. This implementation neither buys a subscription nor verifies the current account's entitlement.

## Configure and run

Set `SIMILARWEB_API_KEY` in the worker's private environment alongside its existing configuration, then restart the worker. Keep the key server-side. The worker does not load an `.env` file automatically. An absent key produces an explicit `unavailable` report without contacting Similarweb.

The authenticated worker accepts `POST /v1/customer-research` with exactly these JSON fields:

```json
{
  "request_id": "customer-meeting-2026-08-001",
  "domain": "example.com",
  "month": "2026-08"
}
```

This is an illustrative payload, not an executed lookup or recommended customer target. Enter the actual customer domain and a past complete month in the meeting interface. Domain input is a DNS name, without a URL scheme, path, query, credentials or port. Uppercase names and a leading `www.` are normalized. The month must be explicit; the interface can default it to the last completed UTC month. Availability can lag behind the calendar.

The client makes one HTTPS request to the fixed `api.similarweb.com` visits endpoint. It sets both provider date parameters to the selected month, country to `world`, and granularity to `monthly`. Redirects and automatic retries are disabled. Connect/read timeouts and a 128 KiB response limit bound the request. No customer-provided URL is fetched.

## Reports and retries

`proofrun.research.v1` reports contain the request ID, research ID, normalized domain, exact period, observation time, provider, source links, limitations, normalized-request hash, and a hash of the received successful HTTP body. Raw responses are discarded because provider metadata can include API credentials.

- `completed`: a valid estimated-visits metric for the requested month; zero is a valid estimate.
- `no_data`: an empty result or null visit estimate, never converted to zero.
- `unavailable`: missing configuration, denied access, rate/credit limits, timeout, network failure, malformed data, or interrupted execution. Errors use fixed safe text.

Reports and normalized requests are stored privately under the worker artifact directory's `customer-research` subdirectory. A request ID permanently identifies its normalized inputs. Repeating it returns the persisted report and makes no additional provider request, including after a restart. Reusing it with different inputs yields HTTP 409. A new explicit research request uses a new ID and may consume another credit.

A durable claim is written before contacting the provider. If the process stops during the call, the same ID reports an interrupted lookup instead of repeating a possibly paid request. A report with `not_configured` also remains cached after configuration changes; explicitly start a new lookup to try again. Reports do not refresh automatically.

## Validation and current evidence

```bash
python3.12 -m pytest tests/test_proofrun_research.py -q
```

The offline tests use a synthetic API key and mocked transport. They cover input rejection, month/domain binding, no-data and zero semantics, secret exclusion, provider failures, response bounds, conflicting IDs, simultaneous requests, persistence, and interruption recovery. Passing them does not establish live Similarweb access, purchased API entitlement or actual customer traffic. Live validation still needs a configured account and the user's chosen customer domain.
