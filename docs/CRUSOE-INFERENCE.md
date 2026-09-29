# Crusoe secondary repair proposals

The selected hackathon architecture keeps OpenRouter as the primary model provider and executes Docker verification locally. Crusoe provides serverless inference for the registered Python/Pydantic fixture's second repair attempt. It does not host the worker.

The flow is:

1. Reproduce the registered regression with native Docker execution.
2. Ask OpenRouter for repair attempt 1.
3. Independently verify it. If it passes, stop without spending Crusoe credits.
4. If completed verification rejects it and the request permits two attempts, send the original bounded source, requirements, observations and rejection feedback to Crusoe.
5. Verify Crusoe's candidate with the same protected tests and controls. Retain `gateway: crusoe`, requested/returned model IDs, operation ID and hashes in proposal evidence.

Provider failures do not trigger hidden retries or substitute models. A missing Crusoe key leaves the second repair unavailable and preserves the reproduced finding. Selecting only one attempt never contacts Crusoe. Generic release investigation and failure research remain on OpenRouter; this adapter supports only the registered fixture. BAND, when enabled, still gates both attempts.

## Configuration

Keep existing OpenRouter values in the main ignored `.env` and add:

```dotenv
PROOFRUN_SECOND_REPAIR_PROVIDER=crusoe
CRUSOE_API_KEY=
PROOFRUN_CRUSOE_MODEL=deepseek-ai/Deepseek-V4-Flash
```

Store the Intelligence API key privately in `CRUSOE_API_KEY`; never paste it into chat or commit it. This model ID was shown by the account console's code example. A different model must be selected explicitly. The API host is fixed to `https://api.inference.crusoecloud.com/v1/chat/completions`; redirects, ambient proxies and implicit HTTP retries are disabled. Request output is capped at 4,096 tokens and the operation at 45 seconds. These bounds limit requests, not total lifetime spend: monitor the account's available credits. No payment method or paid hosting is configured by this change.

Reload the main `.env` in the worker environment and restart the fixture worker to activate the selection. The adapter does not automatically load configuration files. For deployments without this setting, existing two-attempt OpenRouter behavior remains available.

The collector defaults to requiring an OpenRouter accepted repair. To specifically require a Crusoe accepted repair, run with `--repair --expected-target local --expected-repair-provider crusoe`. A first-attempt OpenRouter success correctly does not satisfy that Crusoe evidence gate. Do not deliberately weaken tests or fabricate a rejection to claim a live sponsor contribution.

## Validation status

76 focused tests passed across `test_crusoe_repair.py`, `test_proofrun_repair.py` and `test_proofrun_service.py`. Provider transport in these tests is mocked. This establishes routing and validation behavior, not live Crusoe model compatibility, inference or a verified generated repair. Live validation remains pending account key setup. No running worker has been restarted by this change.

Official reference: https://docs.crusoecloud.com/serverless-inference/index.html
