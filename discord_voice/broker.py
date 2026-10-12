"""Host-owned selected Codex CLI subscription call boundary.

No resolver, files, environment inheritance, login, refresh, or executor here.
The registered native host supplies one atomic *already selected* CLI snapshot.
This is an in-process capability API, not a route or user-argument attestation.
"""
from dataclasses import dataclass

# Codex 0.162.1 RealtimeVoicesList.v1 is also the V3 admitted voice set.
V3_VOICES = frozenset(('juniper', 'maple', 'spruce', 'ember', 'vale', 'breeze', 'arbor', 'sol', 'cove'))


class ScopeRefused(RuntimeError):
    """Nonsecret capability refusal safe to expose to the native controller."""


@dataclass(frozen=True, repr=False)
class CLISnapshot:
    profile: str
    hermes_home: str
    codex_home: str
    mapping_verified: bool
    account_id: str
    user_id: str
    workspace_id: str
    workspace_account: bool
    generation: str
    auth_mode: str
    access_token: str
    expires_at: float
    provider: str
    base_url: str
    model: str
    voice: str
    version: str
    selection_source: str
    routing: str
    managed_requirements: bool

    def __repr__(self):
        return '<CLISnapshot redacted>'


@dataclass(frozen=True, repr=False)
class CLIRecord:
    """Atomic record delivered by the registered host's existing CLI owner.

    effective_config must be native CLI's already-resolved config, not a guessed
    config.toml alone (layers, managed policy and env overrides matter). The host
    attests mapping/routing only after verifying them. No filesystem reads here.
    """
    profile: str
    hermes_home: str
    codex_home: str
    mapping_verified: bool
    generation: str
    auth_json: bytes
    effective_config: dict
    routing: str
    managed_requirements: bool
    # Only the owner adapter may attest this pinned existing consumer provenance.
    consumer_source: str | None = None

    def __repr__(self):
        return '<CLIRecord redacted>'


def snapshot_from_cli_record(record):
    """Read-only decoder of pinned 0.162.1 auth/config shapes; no resolver.

    JWT claims are identity consistency checks, NOT signature verification or
    an authority grant. Authority is the trusted registered credential owner.
    Unknown native/managed routing is refused. Ordinary voice/model defaults
    require the trusted owner's explicit pinned existing-Desktop-V3 provenance;
    records without that provenance stay strict. Effective config is not rewritten.
    """
    import base64
    import hashlib
    import json
    result = None
    try:
        if not isinstance(record, CLIRecord) or len(record.auth_json) > 262144:
            raise ValueError()
        config_json = json.dumps(record.effective_config, sort_keys=True).encode()
        if len(config_json) > 262144:
            raise ValueError()
        config = json.loads(config_json)
        desktop = record.consumer_source == 'desktop-v3/pinned-codex-0.162.1'
        if (config.get('model_provider') not in ((None, 'openai') if desktop else ('openai',)) or config.get('model_providers')
                or config.get('chatgpt_base_url') or config.get('experimental_realtime_ws_base_url')
                or config.get('experimental_realtime_ws_backend_prompt')
                or config.get('agent_identity_authapi_base_url')):
            raise ValueError()
        realtime = config.get('realtime') or {} if desktop else config['realtime']
        if (not isinstance(realtime, dict)
                or realtime.get('version') not in ((None, 'v3') if desktop else ('v3',))
                or realtime.get('transport') not in ((None, 'webrtc') if desktop else ('webrtc',))
                or realtime.get('type') not in ((None, 'conversational') if desktop else ('conversational',))):
            raise ValueError()
        # Source-proven native V3 model and existing Desktop voice defaults;
        # text model is deliberately not used. The effective config is untouched.
        model = config.get('experimental_realtime_ws_model')
        voice = realtime.get('voice')
        if desktop:
            model = model if model is not None else 'gpt-live-1-codex'
            voice = voice if voice is not None else 'cove'
        if not isinstance(model, str) or not model.strip() or voice not in V3_VOICES:
            raise ValueError()
        auth = json.loads(record.auth_json)
        if auth.get('auth_mode') not in (None, 'chatgpt') or auth.get('OPENAI_API_KEY') is not None:
            raise ValueError()
        if any(auth.get(k) is not None for k in ('agent_identity', 'personal_access_token', 'bedrock_api_key', 'bedrock_access_keys')):
            raise ValueError()
        tokens = auth['tokens']
        def claims(token):
            pieces = token.split('.')
            if len(pieces) != 3 or not all(pieces):
                raise ValueError()
            return json.loads(base64.b64decode(pieces[1] + '=' * (-len(pieces[1]) % 4), altchars=b'-_', validate=True))
        identity = claims(tokens['id_token'])['https://api.openai.com/auth']
        access = claims(tokens['access_token'])
        account = tokens['account_id']
        if account != identity['chatgpt_account_id'] or identity.get('chatgpt_account_is_fedramp', False):
            raise ValueError()
        access_identity = access.get('https://api.openai.com/auth', {})
        if (access_identity.get('chatgpt_account_id', account) != account
                or access_identity.get('chatgpt_user_id', identity['chatgpt_user_id']) != identity['chatgpt_user_id']):
            raise ValueError()
        plan = identity['chatgpt_plan_type']
        if plan not in ('free', 'plus', 'pro', 'business', 'enterprise', 'edu', 'team'):
            raise ValueError()
        result = CLISnapshot(
            profile=record.profile, hermes_home=record.hermes_home, codex_home=record.codex_home,
            mapping_verified=record.mapping_verified, account_id=account,
            user_id=identity['chatgpt_user_id'], workspace_id=account,
            workspace_account=plan in ('business', 'enterprise', 'edu', 'team'),
            generation=record.generation + ':' + hashlib.sha256(record.auth_json + config_json).hexdigest(),
            auth_mode='chatgpt', access_token=tokens['access_token'], expires_at=access['exp'],
            provider='openai', base_url='https://chatgpt.com/backend-api/codex', model=model, voice=voice,
            version='v3', selection_source='codex-cli-effective', routing=record.routing,
            managed_requirements=record.managed_requirements)
    except Exception:
        pass
    if result is None:
        raise ScopeRefused('selected CLI auth/config unsupported')
    return result


@dataclass(frozen=True)
class Capability:
    model: str
    voice: str
    version: str = 'v3'
    media: str = 'opus-48000'
    pcm_rate: int = 24000
    pcm_channels: int = 1
    execution: str = 'unavailable'


@dataclass(frozen=True, repr=False)
class HTTPAnswer:
    status: int
    sdp: str
    location: str | None


@dataclass(frozen=True)
class NegotiatedCall:
    sdp: str
    call_id: str
    capability: Capability


def validate_sdp(sdp):
    if (not isinstance(sdp, str) or len(sdp) > 131072 or not sdp.startswith('v=0\r\n')
            or 'm=audio ' not in sdp or 'opus/48000/2' not in sdp.lower()
            or 'webrtc-datachannel' not in sdp):
        raise ScopeRefused('negotiated Opus and events SDP required')


async def post_json(url, headers, payload):
    """Server-side HTTP primitive; broker alone selects destination/auth.

    No redirects, proxy/env/netrc lookup, retries, refresh, or error-body logging.
    Also exercised against an explicitly fake localhost provider in offline tests.
    """
    import aiohttp
    async with aiohttp.ClientSession(trust_env=False,
                                    timeout=aiohttp.ClientTimeout(total=20)) as client:
        async with client.post(url, headers=headers, json=payload, allow_redirects=False) as response:
            if response.status not in (200, 201):
                return HTTPAnswer(response.status, '', None)
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > 131072:
                    raise ScopeRefused('subscription answer too large')
            return HTTPAnswer(response.status, body.decode('utf-8'), response.headers.get('Location'))


class SubscriptionBroker:
    def __init__(self, registered_host, selected_cli_snapshot, *, http_post=None):
        self._host = registered_host
        self._snapshot = selected_cli_snapshot
        self._http_post = http_post or post_json
        self._bindings = {}
        self._adapter_scopes = {}

    def _read(self):
        snapshot = None
        try:
            snapshot = self._snapshot()
        except Exception:
            pass
        if isinstance(snapshot, CLIRecord):
            snapshot = snapshot_from_cli_record(snapshot)
        if not isinstance(snapshot, CLISnapshot):
            raise ScopeRefused('selected CLI snapshot unavailable')
        return snapshot

    def _validate(self, s):
        import math
        import time
        for value in (s.profile, s.hermes_home, s.codex_home, s.generation,
                      s.access_token, s.account_id, s.user_id, s.workspace_id,
                      s.model, s.voice):
            if (not isinstance(value, str) or not value or value != value.strip()
                    or len(value) > 16384 or any(ord(c) < 32 for c in value)):
                raise ScopeRefused('selected CLI capability invalid')
        if len(s.model) > 256:
            raise ScopeRefused('selected CLI model metadata too large')
        if (s.mapping_verified is not True or not s.profile or not s.codex_home or not s.hermes_home
                or s.auth_mode != 'chatgpt' or not s.access_token
                or not s.account_id or not s.user_id or not s.workspace_id
                or not s.generation or not isinstance(s.workspace_account, bool)
                or type(s.expires_at) not in (int, float)
                or not math.isfinite(s.expires_at) or s.expires_at <= time.time() + 30
                or s.provider != 'openai'
                or s.base_url != 'https://chatgpt.com/backend-api/codex'
                or s.routing != 'default-chatgpt' or s.managed_requirements is not False
                or s.selection_source != 'codex-cli-effective'
                or not s.model or s.voice not in V3_VOICES or s.version != 'v3'):
            raise ScopeRefused('selected CLI capability unsupported or expired')

    def register(self, host, *, adapter_scope=None):
        if host is not self._host:
            raise ScopeRefused('registered host required')
        snapshot = self._read()
        self._validate(snapshot)
        if adapter_scope is not None:
            self._match_adapter(snapshot, adapter_scope)
        binding = object()
        self._bindings[binding] = snapshot
        self._adapter_scopes[binding] = adapter_scope
        return binding

    @staticmethod
    def _match_adapter(snapshot, scope):
        if (str(getattr(scope, 'home', '')) != snapshot.hermes_home
                or getattr(scope, 'profile', '') not in
                ((None, 'default') if snapshot.profile == 'default' else (snapshot.profile,))):
            raise ScopeRefused('registered native adapter scope mismatch')

    def preflight_adapter(self, binding, scope):
        if scope is None or self._adapter_scopes.get(binding) is not scope:
            raise ScopeRefused('registered native adapter required')
        capability = self.preflight(binding)
        self._match_adapter(self._bindings[binding], scope)
        return capability

    def preflight(self, binding):
        if binding not in self._bindings:
            raise ScopeRefused('registered binding required')
        try:
            current = self._read()
            if current != self._bindings[binding]:
                raise ScopeRefused('selected CLI scope changed')
            self._validate(current)
        except ScopeRefused:
            self._bindings.pop(binding, None)
            raise
        return Capability(model=current.model, voice=current.voice)

    async def negotiate(self, binding, offer, persona, voice=None):
        import asyncio
        import re
        self.preflight(binding)
        s = self._bindings[binding]
        validate_sdp(offer)
        if (not isinstance(persona, str) or not persona.strip() or len(persona) > 32768
                or voice not in (None, s.voice)):
            raise ScopeRefused('host persona and unchanged selected voice required')
        headers = {'Authorization': 'Bearer ' + s.access_token,
                   'ChatGPT-Account-ID': s.account_id,
                   'Content-Type': 'application/json', 'OpenAI-Alpha': 'quicksilver=v2'}
        payload = {'sdp': offer, 'session': {'model': s.model, 'instructions': persona,
                   'audio': {'output': {'voice': s.voice}}, 'delegation': {'type': 'client'}}}
        answer = None
        try:
            async with asyncio.timeout(20):
                answer = await self._http_post(
                    s.base_url + '/realtime/calls?intent=quicksilver&architecture=avas', headers, payload)
        except Exception:
            pass
        # Never expose HTTP exceptions, request headers, response bodies or redirects.
        if not isinstance(answer, HTTPAnswer) or answer.status not in (200, 201):
            raise ScopeRefused('subscription negotiation failed')
        self.preflight(binding)
        validate_sdp(answer.sdp)
        if not isinstance(answer.location, str) or len(answer.location) > 4096:
            raise ScopeRefused('subscription call identity missing')
        call_id = next((part for part in reversed(answer.location.split('?')[0].split('/'))
                        if re.fullmatch(r'rtc_[A-Za-z0-9_-]{1,128}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', part)), None)
        if call_id is None or s.access_token in answer.sdp or s.access_token in call_id:
            raise ScopeRefused('subscription answer invalid')
        return NegotiatedCall(answer.sdp, call_id, Capability(model=s.model, voice=s.voice))
