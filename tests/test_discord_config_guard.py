"""The selected-CLI config guard must read values, not presence.

The CLI reports its OWN defaults in the effective config (e.g.
``chatgpt_base_url`` = https://chatgpt.com/backend-api/) and grows new keys
between releases. Treating "set" as "overridden", or requiring a closed key
allowlist, refuses a healthy owner: the Discord lane answered
"selected CLI owner/config preflight refused" for a config the CLI chose itself.
"""
import importlib
import sys

import pytest

from test_discord_pcm import ROOT

sys.path.insert(0, str(ROOT))


def guard():
    return importlib.import_module('discord_voice.config_keys')


# The shapes a real 0.162.1 owner presented: the CLI's own default endpoint plus
# keys the pinned allowlist had never seen.
REAL_OWNER_CONFIG = {
    'model': 'gpt-5.6-sol',
    'model_provider': None,
    'cli_auth_credentials_store': 'file',
    'realtime': None,
    'experimental_realtime_ws_model': None,
    'chatgpt_base_url': 'https://chatgpt.com/backend-api/',
    'experimental_thread_store_endpoint': 'http://127.0.0.1:0/thread-store',
    'js_repl_node_module_dirs': [],
    'js_repl_node_path': '/usr/bin/node',
}


def test_real_owner_defaults_are_not_an_override():
    assert guard().routing_override_reason(REAL_OWNER_CONFIG) is None


@pytest.mark.parametrize('value', [
    'https://alternate.invalid',
    'https://chatgpt.com.evil.example/backend-api/',
    'http://chatgpt.com/backend-api/',
    'https://127.0.0.1:8443/backend-api/',
])
def test_non_canonical_chatgpt_endpoints_are_refused(value):
    config = dict(REAL_OWNER_CONFIG, chatgpt_base_url=value)
    assert guard().routing_override_reason(config) is not None


@pytest.mark.parametrize('key', [
    'model_providers',
    'experimental_realtime_ws_base_url',
    'experimental_realtime_ws_backend_prompt',
    'responses_api_metadata',
    'mcp_enterprise_managed_auth',
])
def test_deliberate_overrides_are_still_refused(key):
    config = dict(REAL_OWNER_CONFIG, **{key: {'anything': True}})
    assert guard().routing_override_reason(config) is not None


def test_unset_endpoints_are_accepted():
    config = dict(REAL_OWNER_CONFIG)
    config.pop('chatgpt_base_url')
    assert guard().routing_override_reason(config) is None
