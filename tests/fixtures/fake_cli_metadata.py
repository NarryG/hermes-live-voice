"""EXPLICIT OFFLINE FAKE Codex metadata child; no providers or tasks."""
import json
import os
from pathlib import Path
import sys

home = Path(os.environ['CODEX_HOME'])
fixture = home / 'FAKE-METADATA.json'
assert fixture.is_file() and home.name.startswith('FAKE-')
settings = json.loads(fixture.read_text())
if settings.get('renew_auth'):
    (home / 'auth.json').write_text(json.dumps(settings['renew_auth']))
    (home / 'auth.json').chmod(0o600)
for line in sys.stdin:
    req = json.loads(line)
    method = req['method']
    with (home / 'FAKE-REQUESTS.jsonl').open('a') as log:
        log.write(json.dumps(req) + '\n')
    assert method in {'initialize', 'initialized', 'account/read', 'config/read', 'configRequirements/read'}
    if method == 'initialized':
        continue
    # Pinned protocol uses Option<()>; {} is NOT a valid unit value.
    if method == 'configRequirements/read' and req.get('params') is not None:
        print(json.dumps({'id':req['id'], 'error': {'code':-32602, 'message':'FAKE schema rejects nonunit params'}}), flush=True)
        continue
    settings = json.loads(fixture.read_text())
    result = {
        'initialize': {'userAgent': 'codex_cli_rs/0.162.1 (FAKE OFFLINE)', 'codexHome': str(home),
                       'platformFamily': 'unix', 'platformOs': 'linux'},
        'account/read': settings['account'],
        'config/read': settings['config'],
        'configRequirements/read': settings['requirements'],
    }[method]
    print(json.dumps({'id': req['id'], 'result': result}), flush=True)
