"""Owning-host Codex metadata adapter; loaded lazily after native consent.

The metadata child uses normal native cache/loading/existing-login recovery. It
never receives a thread, realtime, login, account-selection or execution request.
Secrets stay here and in the broker, never in a Discord/media object or log.
Source contract: Codex 0.162.1, 092d3acd6bec3e3a14bdc7e7a2810ab628ab759d.
"""
import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
from types import MappingProxyType

from .broker import CLIRecord, ScopeRefused, snapshot_from_cli_record
from .config_keys import CONFIG_KEYS, routing_override_reason

POLL_INTERVAL = 2.0
METADATA_TTL = 8.0
_ENV_KEYS = frozenset(('HOME', 'PATH', 'CODEX_HOME', 'HERMES_HOME', 'OPENAI_API_KEY',
                      'OPENAI_BASE_URL', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE'))
_MAX = 262144
_METHODS = frozenset(('initialize', 'account/read', 'config/read', 'configRequirements/read'))


@dataclass(frozen=True, repr=False)
class CapturedSelection:
    home: Path
    cwd: Path
    env: object
    codex_home: Path
    live_environment: bool

    def __repr__(self):
        return '<CapturedSelection redacted>'


def _selection_env(env):
    return {key: value for key, value in env.items()
            if key in _ENV_KEYS or key.startswith(('CODEX_', 'OPENAI_', 'CHATGPT_'))}


def capture_selection(home, *, environ=None, cwd=None):
    """Capture producer process context, without reading credentials/config.

    Desktop _cl_ensure inherits process CWD and HOME/CODEX_HOME. Only the
    hosting-default native adapter may use this ordinary process mapping.
    No satellite mapping is inferred from late environment/default HOME.
    """
    env = dict(os.environ if environ is None else environ)
    producer_cwd = Path(os.getcwd() if cwd is None else cwd).absolute()
    os_home = env.get('HOME')
    codex_home = env.get('CODEX_HOME') or (str(Path(os_home) / '.codex') if os_home else '')
    return CapturedSelection(Path(home).resolve(), producer_cwd, MappingProxyType(env), Path(codex_home), environ is None)


def _json(data):
    if len(data) > _MAX:
        raise ScopeRefused('selected metadata too large')
    # Duplicate keys are not a stable native snapshot.
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def _claims(token):
    pieces = token.split('.')
    if len(pieces) != 3:
        raise ValueError()
    return _json(base64.b64decode(pieces[1] + '=' * (-len(pieces[1]) % 4), altchars=b'-_', validate=True))


def _identity(raw):
    value = _json(raw)
    if value.get('auth_mode') not in (None, 'chatgpt') or value.get('OPENAI_API_KEY') is not None:
        raise ValueError()
    if any(value.get(k) is not None for k in ('agent_identity', 'personal_access_token', 'bedrock_api_key', 'bedrock_access_keys')):
        raise ValueError()
    tokens = value['tokens']
    claims = _claims(tokens['id_token'])
    identity = claims['https://api.openai.com/auth']
    account, user = identity['chatgpt_account_id'], identity['chatgpt_user_id']
    if not account or not user or tokens['account_id'] != account or identity.get('chatgpt_account_is_fedramp'):
        raise ValueError()
    return account, user, identity['chatgpt_plan_type'], claims.get('email') or claims.get('https://api.openai.com/profile', {}).get('email')


def _file(path, *, secret=False):
    """Bounded no-follow owner file read. No path from a command argument."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError()
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size > _MAX
                or (secret and (before.st_uid != os.getuid() or before.st_mode & 0o077))):
            raise ValueError()
        raw = os.read(fd, _MAX + 1)
        after = os.fstat(fd)
        def stable(value):
            return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                    value.st_ctime_ns, value.st_mode, value.st_uid, value.st_nlink)
        # Reading can update atime: it is not a credential-generation change.
        if len(raw) > _MAX or stable(after) != stable(before):
            raise ValueError()
        return raw
    finally:
        os.close(fd)


def _refusal_reason(exc):
    """Short, non-secret reason the selected owner refused, for the operator.

    prepare() refuses by returning False, so without this the lane answered
    "preflight refused" with no cause and a healthy owner looked like a missing
    feature. Only short fixed reason strings and exception type names are kept:
    nothing that could quote credential material.
    """
    text = str(exc).strip()
    if isinstance(exc, (ScopeRefused, ValueError)) and 0 < len(text) <= 120:
        return text
    return type(exc).__name__


class SelectedCLIOwner:
    """Persistent bounded metadata client, scoped to actual native registration."""
    def __init__(self, host, scope, captured, *, metadata_command=None, scope_guard=None):
        self._host, self._scope, self._captured = host, scope, captured
        self._scope_guard = scope_guard
        self._command = metadata_command  # internal offline fixture injection only
        self._proc = None
        self._snapshot = None
        self._seq = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._refusal = None
        self._invalid = False
        self._paths = ()
        self._stamps = None
        self._watcher = None
        self._metadata_value = None
        self._attested_at = 0.
        self._binary_entry = None
        self._binary_source = None

    def __repr__(self):
        return '<SelectedCLIOwner redacted>'

    @property
    def refusal_reason(self):
        """Short non-secret cause of the last refused prepare, if any."""
        return self._refusal

    def _scope_check(self, scope):
        c = self._captured
        if (scope is not self._scope or scope.home != c.home or scope.profile is not None
                or (self._scope_guard is not None and self._scope_guard() is not True)
                or (c.live_environment and _selection_env(os.environ) != _selection_env(c.env))
                or not c.codex_home.is_absolute() or not c.cwd.is_absolute()):
            raise ScopeRefused('selected CLI owning profile mapping unavailable')

    async def _rpc(self, method, params):
        if method not in _METHODS:
            raise ScopeRefused('metadata method refused')
        self._seq += 1
        rid = self._seq
        request = {'id': rid, 'method': method}
        if params is not None:
            request['params'] = params
        self._proc.stdin.write(json.dumps(request).encode() + b'\n')
        await self._proc.stdin.drain()
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                raise ScopeRefused('metadata process closed')
            value = _json(line)
            if value.get('id') == rid:
                if 'error' in value or not isinstance(value.get('result'), dict):
                    raise ScopeRefused('selected metadata unavailable')
                return value['result']
            # Notifications are metadata only; no request handler/executor exists.
            if 'id' in value:
                raise ScopeRefused('metadata child requested unsupported action')

    def _binary(self):
        c = self._captured
        binary = shutil.which('codex', path=c.env.get('PATH', ''))
        if not binary:
            home = Path(c.env['HOME'])
            binary = next((str(p) for p in (home/'.npm-global/bin/codex', Path('/usr/local/bin/codex'),
                            home/'.local/bin/codex', Path('/usr/bin/codex')) if p.is_file() and os.access(p, os.X_OK)), None)
        if not binary:
            raise ScopeRefused('captured selected CLI binary unavailable')
        return (str(Path(binary).absolute()), 'app-server', '--listen', 'stdio://')

    async def _metadata(self, *, renew=False):
        c = self._captured
        account = await self._rpc('account/read', {'refreshToken': renew})
        config = await self._rpc('config/read', {'includeLayers': True, 'cwd': str(c.cwd)})
        requirements = await self._rpc('configRequirements/read', None)
        return account, config, requirements

    def _source_paths(self, response):
        c = self._captured
        layers, origins = response['layers'], response['origins']
        if not layers or len(layers) > 64:
            raise ValueError()
        paths, known, user_found = [], [], False
        for layer in layers:
            name, version, config = layer['name'], layer['version'], layer['config']
            if (not isinstance(version, str) or not version or len(version) > 256
                    or not isinstance(config, dict) or set(config) - CONFIG_KEYS):
                raise ValueError()
            kind = name['type']
            if kind == 'user':
                if set(name) != {'type', 'file', 'profile'} or name['profile'] is not None or name['file'] != str(c.codex_home/'config.toml'):
                    raise ValueError()
                user_found = True
                paths.append(Path(name['file']))
            elif kind == 'system':
                if set(name) != {'type', 'file'}:
                    raise ValueError()
                paths.append(Path(name['file']))
            elif kind == 'project':
                if set(name) != {'type', 'dotCodexFolder'}:
                    raise ValueError()
                folder = Path(name['dotCodexFolder'])
                if folder.name != '.codex' or folder.parent not in (c.cwd, *c.cwd.parents):
                    raise ValueError()
                paths.append(folder/'config.toml')
            elif kind == 'sessionFlags':
                if config or set(name) != {'type'}:
                    raise ValueError()
            else:
                # Managed/cloud/MDM policy cannot be preserved by this direct
                # broker yet, so do not silently drop it or guess an attestation.
                raise ValueError()
            known.append({'name': name, 'version': version})
        if not user_found or any(not p.is_absolute() for p in paths):
            raise ValueError()
        if any(not isinstance(key, str) or value not in known for key, value in origins.items()):
            raise ValueError()
        return tuple(paths)

    def _record(self, raw, metadata):
        c = self._captured
        account, response, requirements = metadata
        config = response['config']
        if (not isinstance(response.get('layers'), list) or not isinstance(response.get('origins'), dict)
                or not isinstance(config, dict)):
            raise ValueError('config layers/origins shape')
        if config.get('cli_auth_credentials_store') not in (None, 'file'):
            raise ValueError('non-file credential store')
        if requirements.get('requirements', 'unknown') is not None:
            raise ValueError('managed requirements present')
        override = routing_override_reason(config)
        if override is not None:
            raise ValueError(override)
        if config.get('forced_login_method') not in (None, 'chatgpt'):
            raise ValueError()
        self._source_paths(response)
        aid, uid, plan, email = _identity(raw)
        routed = account['workspaceRouting']
        info = account['account']
        if (info['type'] != 'chatgpt' or info['planType'] != plan or not email or info['email'] != email
                or account['requiresOpenaiAuth'] is not True
                or routed['chatgptAccountId'] != aid or routed['backendOrigin'] != 'https://chatgpt.com'
                or routed['accountRoutingOverride'] != 'NO_CONSTRAINT'):
            raise ValueError()
        generation = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
        return CLIRecord('default', str(c.home), str(c.codex_home), True, generation, raw, config,
                         'default-chatgpt', False, 'desktop-v3/pinned-codex-0.162.1')

    async def prepare(self, scope):
        async with self._lock:
            self._refusal = None
            try:
                self._scope_check(scope)
                if self._closed:
                    return False
                if self._snapshot is not None:
                    self.snapshot()
                    return True
                c = self._captured
                if any(c.env.get(key) for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN', 'OPENAI_BASE_URL', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE')):
                    raise ValueError()
                self._paths = (c.home, c.codex_home, c.cwd, c.codex_home / 'auth.json', c.codex_home / 'config.toml')
                self._file_stamps()  # reject unsafe owner paths BEFORE native loading
                before = _file(c.codex_home / 'auth.json', secret=True)
                identity = _identity(before)
                command = self._command or self._binary()
                self._binary_entry = Path(command[0]).absolute()
                self._binary_source = self._binary_entry.resolve(strict=True)
                self._paths += (self._binary_source,)
                self._proc = await asyncio.create_subprocess_exec(*command, cwd=str(c.cwd), env=dict(c.env),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=_MAX + 1)
                async with asyncio.timeout(25):
                    init = await self._rpc('initialize', {'clientInfo': {'name': 'hermes-discord-metadata', 'version': '0.3.0'},
                                                         'capabilities': {'experimentalApi': True}})
                    if (init.get('codexHome') != str(c.codex_home)
                            or not re.search(r'/0\.162\.1(?:\s|$)', init.get('userAgent', ''))):
                        raise ValueError()
                    self._proc.stdin.write(b'{"method":"initialized"}\n')
                    await self._proc.stdin.drain()
                    expiry = _claims(_json(before)['tokens']['access_token'])['exp']
                    if type(expiry) not in (int, float):
                        raise ValueError()
                    metadata = await self._metadata(renew=expiry <= time.time() + 60)
                    after = _file(c.codex_home / 'auth.json', secret=True)
                    if _identity(after) != identity:
                        raise ValueError()
                    self._paths += self._source_paths(metadata[1])
                    stable_files = self._file_stamps()
                    if (await self._metadata() != metadata
                            or _file(c.codex_home / 'auth.json', secret=True) != after
                            or self._file_stamps() != stable_files):
                        raise ValueError()
                    self._snapshot = snapshot_from_cli_record(self._record(after, metadata))
                    self._stamps = stable_files
                    self._metadata_value = metadata
                    self._attested_at = time.monotonic()
                    self._watcher = asyncio.create_task(self._watch())
                return True
            except Exception as exc:
                self._refusal = _refusal_reason(exc)
                await self.close()
                return False

    async def _watch(self):
        try:
            while not self._closed:
                await asyncio.sleep(POLL_INTERVAL)
                async with self._lock:
                    self.snapshot()
                    async with asyncio.timeout(METADATA_TTL / 2):
                        metadata = await self._metadata()
                    # Never extend cached truth across auth/config/layer/policy/
                    # routing changes; even same-owner renewal revokes old media.
                    self.snapshot()
                    if metadata != self._metadata_value:
                        raise ValueError()
                    self._attested_at = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._invalid = True
            await self.close()

    def _file_stamps(self):
        stamps = []
        for path in self._paths:
            for parent in path.parents:
                if parent.is_symlink():
                    raise ValueError()
            try:
                value = path.lstat()
            except FileNotFoundError:
                if path.name != 'config.toml':
                    raise
                stamps.append(None)
                continue
            if stat.S_ISLNK(value.st_mode) or value.st_mode & 0o022:
                raise ValueError()
            if path == self._captured.codex_home and value.st_uid != os.getuid():
                raise ValueError()
            if path.name == 'auth.json' and (value.st_mode & 0o077 or value.st_uid != os.getuid() or value.st_nlink != 1):
                raise ValueError()
            # Directory mtime changes with normal CLI cache publication; ownership
            # and inode are the fence, not unrelated child-cache writes.
            stamps.append((value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                           None if stat.S_ISDIR(value.st_mode) else (value.st_size, value.st_mtime_ns, value.st_ctime_ns)))
        return tuple(stamps)

    def snapshot(self):
        if self._snapshot is None:
            raise ScopeRefused('selected CLI metadata not ready')
        try:
            self._scope_check(self._scope)
            if (self._closed or self._invalid or self._snapshot is None
                    or time.monotonic() - self._attested_at > METADATA_TTL
                    or self._binary_entry.resolve(strict=True) != self._binary_source
                    or self._proc.returncode is not None or self._file_stamps() != self._stamps):
                raise ValueError()
        except Exception:
            self._invalid = True
            raise ScopeRefused('selected CLI metadata not ready or changed') from None
        return self._snapshot

    async def close(self):
        self._closed = True
        self._snapshot = None
        if self._watcher and self._watcher is not asyncio.current_task():
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 2)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()


class SelectedLaneFactory:
    """Implementation admission is not account readiness or live entitlement."""
    admitted = True
    supported_version = 'v3'
    processing_disclosure = ('Admitted participant audio goes to the already-selected Codex CLI '
        'ChatGPT subscription via direct WebRTC. Normal native CLI metadata/cache loading '
        'and renewal of its existing login may occur after opt-in. No task executor is attached.')

    def __init__(self, owner, *, http_post=None, startup_timeout=10):
        self._owner = owner
        self._http_post, self._timeout = http_post, startup_timeout
        self._factory = None
        self._lock = asyncio.Lock()

    @property
    def account_ready(self):
        return self._factory is not None and self._factory.admitted is True

    @property
    def media_current(self):
        # Native player/UDP guards must fence already-buffered audio synchronously,
        # not wait for an asyncio poll to notice a changed owner/config generation.
        return self.account_ready

    @property
    def refusal_reason(self):
        """Short non-secret cause when this lane last refused to prepare."""
        return getattr(self._owner, 'refusal_reason', None)

    def accepts_voice(self, voice):
        try:
            return self.account_ready and voice in (None, self._owner.snapshot().voice)
        except ScopeRefused:
            return False

    async def preflight(self, scope):
        from .broker import SubscriptionBroker
        from .webrtc import lane_factory
        async with self._lock:
            if scope is not self._owner._scope:
                return False
            if not await self._owner.prepare(scope):
                return False
            try:
                if self._factory is None:
                    broker = SubscriptionBroker(self._owner._host, self._owner.snapshot, http_post=self._http_post)
                    binding = broker.register(self._owner._host, adapter_scope=scope)
                    self._factory = lane_factory(broker, binding, startup_timeout=self._timeout)
                return await self._factory.preflight(scope)
            except ScopeRefused:
                return False

    def __call__(self):
        if self._factory is None:
            raise ScopeRefused('same-scope owner preflight required before media creation')
        return self._factory()

    async def shutdown(self):
        await self._owner.close()
