"""Synthetic selected-CLI credentials only. No real auth/config reads."""
import asyncio
import dataclasses
import importlib
import time
import pytest


def api():
    try:
        return importlib.import_module('discord_voice.broker')
    except ModuleNotFoundError:
        pytest.fail('Selected-CLI token-opaque broker not implemented')


def fake_snapshot(b):
    return b.CLISnapshot(
        profile='default', hermes_home='/synthetic/FAKE-host', codex_home='/synthetic/FAKE-cli', mapping_verified=True,
        account_id='FAKE-account', user_id='FAKE-user', workspace_id='FAKE-workspace',
        workspace_account=True, generation='FAKE-generation-1',
        auth_mode='chatgpt', access_token='FAKE-NOT-A-REAL-BEARER', expires_at=time.time()+3600,
        provider='openai', base_url='https://chatgpt.com/backend-api/codex',
        model='gpt-live-1-codex', voice='maple', version='v3',
        selection_source='codex-cli-effective', routing='default-chatgpt',
        managed_requirements=False,
    )


def test_host_registration_is_required_before_any_ingress():
    b = api()
    host = object()
    snapshot = fake_snapshot(b)
    broker = b.SubscriptionBroker(host, lambda: snapshot)
    with pytest.raises(b.ScopeRefused, match='binding'):
        broker.preflight(object())
    binding = broker.register(host)
    assert broker.preflight(binding).version == 'v3'
    with pytest.raises(b.ScopeRefused, match='host'):
        broker.register(object())


@pytest.mark.parametrize('change', [
    {'mapping_verified': False}, {'auth_mode': 'apikey'}, {'auth_mode': 'hermes-oauth'},
    {'expires_at': 0}, {'account_id': ''}, {'user_id': ''}, {'workspace_id': ''},
    {'access_token': ''}, {'model': ''}, {'voice': ''}, {'version': 'v2'},
    {'base_url': 'https://api.openai.com/v1'}, {'provider': 'other'},
    {'routing': 'unknown'}, {'managed_requirements': True},
    {'selection_source': 'guessed-default'}, {'codex_home': ''},
    {'hermes_home': ''}, {'voice': 'cedar'},
    {'voice': []}, {'model': ' '}, {'account_id': ' FAKE-account'},
    {'expires_at': True}, {'access_token': b'FAKE-bytes'},
])
def test_unsupported_selection_refuses_registration(change):
    b = api()
    host = object()
    snap = dataclasses.replace(fake_snapshot(b), **change)
    broker = b.SubscriptionBroker(host, lambda: snap)
    with pytest.raises(b.ScopeRefused):
        broker.register(host)


@pytest.mark.parametrize('change', [
    {'account_id': 'FAKE-other'}, {'user_id': 'FAKE-other'},
    {'workspace_id': 'FAKE-other'}, {'workspace_account': False},
    {'generation': 'FAKE-generation-2'}, {'access_token': 'FAKE-rotated'},
    {'codex_home': '/synthetic/FAKE-other'}, {'profile': 'satellite'},
    {'model': 'FAKE-other'}, {'voice': 'cedar'},
])
def test_identity_or_selection_rotation_revokes_pinned_binding(change):
    b = api()
    host = object()
    snap = fake_snapshot(b)
    broker = b.SubscriptionBroker(host, lambda: snap)
    binding = broker.register(host)
    snap = dataclasses.replace(snap, **change)
    with pytest.raises(b.ScopeRefused, match='changed'):
        broker.preflight(binding)
    snap = fake_snapshot(b)
    with pytest.raises(b.ScopeRefused):
        broker.preflight(binding)


def test_unreadable_snapshot_is_a_nonsecret_refusal():
    b = api()
    def unreadable():
        raise OSError('FAKE-NOT-A-REAL-BEARER')
    host = object()
    broker = b.SubscriptionBroker(host, unreadable)
    with pytest.raises(b.ScopeRefused) as error:
        broker.register(host)
    assert 'FAKE' not in str(error.value)
    assert error.value.__cause__ is None


OFFER = 'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=rtpmap:111 opus/48000/2\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n'


def test_negotiation_exact_request_returns_only_nonsecret_result():
    b = api()
    snap = fake_snapshot(b)
    host, seen = object(), []
    async def fake_http(url, headers, payload):
        seen.append((url, headers, payload))
        return b.HTTPAnswer(201, OFFER, '/realtime/calls/rtc_FAKE-call')
    broker = b.SubscriptionBroker(host, lambda: snap, http_post=fake_http)
    binding = broker.register(host)
    result = asyncio.run(broker.negotiate(binding, OFFER, 'Host-owned persona', None))
    url, headers, payload = seen[0]
    assert url == 'https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas'
    assert headers == {'Authorization': 'Bearer '+snap.access_token, 'ChatGPT-Account-ID': snap.account_id,
                       'Content-Type': 'application/json', 'OpenAI-Alpha': 'quicksilver=v2'}
    assert payload == {'sdp': OFFER, 'session': {'model': snap.model, 'instructions': 'Host-owned persona',
                          'audio': {'output': {'voice': snap.voice}}, 'delegation': {'type': 'client'}}}
    assert set(dataclasses.asdict(result)) == {'sdp', 'call_id', 'capability'}
    assert result.call_id == 'rtc_FAKE-call'
    assert snap.access_token not in repr(result)
    assert snap.account_id not in repr(result)
    assert snap.access_token not in repr(snap)


@pytest.mark.parametrize('answer', [
    (401, 'FAKE-NOT-A-REAL-BEARER', None), (302, OFFER, '/rtc_FAKE'),
    (201, OFFER, None), (201, OFFER, '/not-a-call'),
    (201, 'FAKE-NOT-A-REAL-BEARER', '/rtc_FAKE'),
])
def test_invalid_answer_cannot_leak_or_fallback(answer):
    b = api()
    snap, host, calls = fake_snapshot(b), object(), []
    async def fake_http(*args):
        calls.append(1)
        return b.HTTPAnswer(*answer)
    broker = b.SubscriptionBroker(host, lambda: snap, http_post=fake_http)
    binding = broker.register(host)
    with pytest.raises(b.ScopeRefused) as error:
        asyncio.run(broker.negotiate(binding, OFFER, 'Persona', None))
    assert 'FAKE' not in str(error.value)
    assert len(calls) == 1


def test_changed_identity_during_http_refuses_result():
    b = api()
    snap, host = fake_snapshot(b), object()
    async def fake_http(*args):
        nonlocal snap
        snap = dataclasses.replace(snap, account_id='FAKE-rotated')
        return b.HTTPAnswer(201, OFFER, '/rtc_FAKE')
    broker = b.SubscriptionBroker(host, lambda: snap, http_post=fake_http)
    binding = broker.register(host)
    with pytest.raises(b.ScopeRefused, match='changed'):
        asyncio.run(broker.negotiate(binding, OFFER, 'Persona', None))


def test_voice_override_and_invalid_offer_refuse_before_http():
    b = api()
    snap, host = fake_snapshot(b), object()
    async def forbidden(*args):
        pytest.fail('Invalid request reached provider ingress')
    broker = b.SubscriptionBroker(host, lambda: snap, http_post=forbidden)
    binding = broker.register(host)
    for offer, voice in [(OFFER, 'cedar'), ('garbage', None)]:
        with pytest.raises(b.ScopeRefused):
            asyncio.run(broker.negotiate(binding, offer, 'Persona', voice))


def test_actual_http_transport_localhost_fake_provider_redirect_is_not_followed():
    from aiohttp import web
    b = api()
    async def run():
        seen = []
        async def fake_provider(request):
            seen.append((request.headers['Authorization'], await request.json()))
            return web.Response(status=201, text=OFFER, headers={'Location': '/rtc_FAKE-http'})
        async def redirect(request):
            return web.Response(status=302, headers={'Location': '/fake'})
        app = web.Application()
        app.router.add_post('/fake', fake_provider)
        app.router.add_post('/redirect', redirect)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            url = f'http://127.0.0.1:{port}'
            result = await b.post_json(url+'/fake', {'Authorization': 'Bearer FAKE-HTTP'}, {'sdp': OFFER})
            assert result == b.HTTPAnswer(201, OFFER, '/rtc_FAKE-http')
            assert seen == [('Bearer FAKE-HTTP', {'sdp': OFFER})]
            redirected = await b.post_json(url+'/redirect', {}, {})
            assert redirected.status == 302
            assert len(seen) == 1
        finally:
            await runner.cleanup()
    asyncio.run(run())


def synthetic_cli_record(b):
    import base64
    import json
    # Labels mark this as FAKE unsigned JWT test data, never a credential.
    def jwt(claims):
        encode = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')
        return encode({'alg': 'none', 'FAKE': True})+'.'+encode(claims)+'.FAKE-signature'
    identity = {'chatgpt_user_id': 'FAKE-user', 'chatgpt_account_id': 'FAKE-account',
                'chatgpt_plan_type': 'business', 'chatgpt_account_is_fedramp': False}
    auth = {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None, 'tokens': {
        'id_token': jwt({'https://api.openai.com/auth': identity}),
        'access_token': jwt({'exp': int(time.time())+3600, 'https://api.openai.com/auth': identity}),
        'refresh_token': 'FAKE-DO-NOT-USE', 'account_id': 'FAKE-account'}}
    config = {'model_provider': 'openai', 'experimental_realtime_ws_model': 'gpt-live-1-codex',
              'realtime': {'version': 'v3', 'type': 'conversational', 'transport': 'webrtc', 'voice': 'maple'}}
    return b.CLIRecord(profile='default', hermes_home='/synthetic/FAKE-host', codex_home='/synthetic/FAKE-cli',
                       mapping_verified=True, generation='FAKE-generation',
                       auth_json=json.dumps(auth).encode(), effective_config=config,
                       routing='default-chatgpt', managed_requirements=False)


def test_native_cli_source_shapes_are_consumed_without_new_defaults_or_refresh():
    b = api()
    record = synthetic_cli_record(b)
    before = record.auth_json
    snapshot = b.snapshot_from_cli_record(record)
    assert snapshot.account_id == snapshot.workspace_id == 'FAKE-account'
    assert snapshot.user_id == 'FAKE-user' and snapshot.workspace_account is True
    assert snapshot.model == 'gpt-live-1-codex' and snapshot.voice == 'maple'
    assert record.auth_json == before
    assert 'FAKE-DO-NOT-USE' not in repr(snapshot)
    assert 'FAKE-DO-NOT-USE' not in repr(record)


@pytest.mark.parametrize('change', [
    {'experimental_realtime_ws_model': None}, {'model_provider': 'custom'},
    {'realtime': {'voice': 'maple'}}, {'realtime': {'version': 'v2', 'voice': 'maple'}},
    {'model_providers': {'openai': {'base_url': 'https://alternate.invalid'}}},
    {'chatgpt_base_url': 'https://alternate.invalid'},
])
def test_unverified_cli_config_shape_refuses_instead_of_guessing(change):
    b = api()
    record = synthetic_cli_record(b)
    record.effective_config.update(change)
    with pytest.raises(b.ScopeRefused):
        b.snapshot_from_cli_record(record)
