"""Mocked provider boundary checks; no live Crusoe access claimed."""
import json
from unittest.mock import patch
import pytest
from src.proofrun.config import RepairConfig, ConfigurationError
from src.proofrun.contracts import ProposalUnavailable
from src.proofrun.repair import CrusoeRepairClient, propose_patch
from test_proofrun_repair import FakeTransport, FakeResponse, failure_context, envelope, candidate

ENV = {'OPENROUTER_API_KEY': 'synthetic-openrouter-key', 'PROOFRUN_MODEL': 'vendor/primary',
       'CRUSOE_API_KEY': 'synthetic-crusoe-key', 'PROOFRUN_CRUSOE_MODEL': 'deepseek-ai/Deepseek-V4-Flash',
       'PROOFRUN_SECOND_REPAIR_PROVIDER': 'crusoe'}


def test_crusoe_endpoint_provenance_and_bounded_body():
    transport = FakeTransport()
    proposal = CrusoeRepairClient(RepairConfig.crusoe_from_env(ENV), transport).propose_patch(failure_context(), 2)
    url, args = transport.calls[0]
    assert url == 'https://api.inference.crusoecloud.com/v1/chat/completions'
    assert args['headers']['Authorization'] == 'Bearer synthetic-crusoe-key'
    assert args['allow_redirects'] is False
    assert 'provider' not in args['json']
    assert args['json']['max_tokens'] <= 4096
    assert proposal.provenance['gateway'] == 'crusoe'
    assert proposal.provenance['mode'] == 'mock'
    assert ENV['CRUSOE_API_KEY'] not in json.dumps(proposal.provenance)


def test_first_remains_openrouter_second_is_crusoe():
    with patch.dict('os.environ', ENV, clear=True), patch('src.proofrun.repair._live_request', return_value=json.dumps(envelope()).encode()) as call:
        first = propose_patch(failure_context(), 1)
        second = propose_patch(failure_context(), 2)
    assert first.provenance['gateway'] == 'openrouter'
    assert second.provenance['gateway'] == 'crusoe'
    assert [c.args[0].api_key for c in call.call_args_list] == [ENV['OPENROUTER_API_KEY'], ENV['CRUSOE_API_KEY']]


def test_missing_crusoe_key_never_falls_back():
    with patch.dict('os.environ', {**ENV, 'CRUSOE_API_KEY': ''}, clear=True), patch('src.proofrun.repair._live_request') as call:
        with pytest.raises(ProposalUnavailable, match='CRUSOE_API_KEY'):
            propose_patch(failure_context(), 2)
        call.assert_not_called()


def test_crusoe_errors_are_safe_and_do_not_retry():
    transport = FakeTransport(FakeResponse(status=402, raw=b'private provider response'))
    with pytest.raises(ProposalUnavailable, match='Crusoe credit') as exc:
        CrusoeRepairClient(RepairConfig.crusoe_from_env(ENV), transport).propose_patch(failure_context(), 2)
    assert 'private' not in str(exc.value)
    assert len(transport.calls) == 1


def test_crusoe_cannot_change_protected_path_or_return_secret():
    for changes in ({'allowed_path': 'tests/test_controls.py'}, {'rationale': ENV['CRUSOE_API_KEY']}):
        body = envelope({**candidate(), **changes})
        with pytest.raises(ProposalUnavailable):
            CrusoeRepairClient(RepairConfig.crusoe_from_env(ENV), FakeTransport(FakeResponse(body))).propose_patch(failure_context(), 2)


def test_unknown_gateway_rejected_before_credentials_can_be_sent():
    with pytest.raises(ConfigurationError):
        RepairConfig('synthetic-key', 'vendor/model', gateway='https://untrusted.invalid')
