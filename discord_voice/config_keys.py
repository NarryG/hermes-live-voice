from urllib.parse import urlsplit
# Derived from Apache-2.0 Codex 0.162.1 ConfigToml schema,
# revision 092d3acd6bec3e3a14bdc7e7a2810ab628ab759d (see NOTICE.md).
CONFIG_KEYS = frozenset(('agents', 'allow_login_shell', 'allow_symlinked_codex_home', 'analytics', 'approval_policy', 'approvals_reviewer', 'apps', 'apps_mcp_product_sku', 'audio', 'auto_review', 'background_terminal_max_timeout', 'browser_use', 'chatgpt_base_url', 'check_for_update_on_startup', 'cli_auth_credentials_store', 'cloud', 'compact_prompt', 'computer_use', 'daybreak', 'default_permissions', 'desktop', 'developer_instructions', 'disable_paste_burst', 'experimental_compact_prompt_file', 'experimental_realtime_start_instructions', 'experimental_realtime_webrtc_call_base_url', 'experimental_realtime_ws_backend_prompt', 'experimental_realtime_ws_base_url', 'experimental_realtime_ws_model', 'experimental_realtime_ws_startup_context', 'experimental_thread_store', 'experimental_use_unified_exec_tool', 'features', 'feedback', 'file_opener', 'forced_chatgpt_workspace_id', 'forced_login_method', 'ghost_snapshot', 'goals', 'hide_agent_reasoning', 'history', 'hooks', 'include_apps_instructions', 'include_collaboration_mode_instructions', 'include_environment_context', 'include_permissions_instructions', 'instructions', 'log_dir', 'marketplaces', 'mcp_enterprise_managed_auth', 'mcp_oauth_callback_port', 'mcp_oauth_callback_url', 'mcp_oauth_credentials_store', 'mcp_optional_startup_grace_ms', 'mcp_servers', 'memories', 'model', 'model_auto_compact_token_limit', 'model_auto_compact_token_limit_scope', 'model_catalog_json', 'model_context_window', 'model_instructions_file', 'model_post_turn_compact_threshold_percent', 'model_provider', 'model_providers', 'model_reasoning_effort', 'model_reasoning_summary', 'model_verbosity', 'notice', 'notify', 'openai_base_url', 'orchestrator', 'oss_provider', 'otel', 'permissions', 'personality', 'plan_mode_reasoning_effort', 'plugins', 'profile', 'profiles', 'project_doc_fallback_filenames', 'project_doc_max_bytes', 'project_root_markers', 'projects', 'realtime', 'responses_api_metadata', 'review_model', 'sandbox_mode', 'sandbox_workspace_write', 'service_tier', 'shell_environment_policy', 'show_raw_agent_reasoning', 'skills', 'sqlite_home', 'suppress_unstable_features_warning', 'thread_unload_delay_secs', 'tool_output_token_limit', 'tool_suggest', 'tools', 'tui', 'web_search', 'windows'))


CANONICAL_CHATGPT_HOST = 'chatgpt.com'
CANONICAL_OPENAI_HOST = 'api.openai.com'

# A truthy value here is always a deliberate override (custom provider, endpoint,
# injected prompt or managed-auth policy) and never a CLI default, so presence
# alone still refuses.
OVERRIDE_CONFIG_KEYS = (
    'model_providers',
    'agent_identity_authapi_base_url',
    'experimental_realtime_webrtc_call_base_url',
    'experimental_realtime_ws_base_url',
    'experimental_realtime_ws_backend_prompt',
    'experimental_realtime_ws_startup_context',
    'experimental_realtime_start_instructions',
    'responses_api_metadata',
    'mcp_enterprise_managed_auth',
)


def endpoint_is_canonical(value, host):
    """True when `value` is unset, or an https URL on the canonical host.

    The CLI reports its OWN defaults in the effective config (a real owner
    presented chatgpt_base_url = https://chatgpt.com/backend-api/), so presence
    is not an override: only a different scheme or host is.
    """
    if value in (None, ''):
        return True
    if not isinstance(value, str):
        return False
    parts = urlsplit(value)
    return parts.scheme == 'https' and parts.hostname == host


def routing_override_reason(config):
    """Return a short reason when `config` redirects the lane, else None.

    Unknown keys are tolerated: this is a third-party CLI's config surface and it
    grows between releases, while a closed allowlist refused healthy owners. The
    settings that actually decide where the subscription goes are checked by
    value here.
    """
    if not isinstance(config, dict):
        return 'effective config is not an object'
    for key in OVERRIDE_CONFIG_KEYS:
        if config.get(key):
            return 'config override: ' + key
    if not endpoint_is_canonical(config.get('chatgpt_base_url'), CANONICAL_CHATGPT_HOST):
        return 'non-canonical chatgpt_base_url'
    if not endpoint_is_canonical(config.get('openai_base_url'), CANONICAL_OPENAI_HOST):
        return 'non-canonical openai_base_url'
    return None
