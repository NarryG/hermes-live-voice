"""Offline bootstrap tests; no real package installation or voice effects."""
import importlib


def test_bootstrap_module_is_available():
    import importlib.util
    assert importlib.util.find_spec('discord_voice.bootstrap') is not None


def test_ready_install_is_noop():
    bootstrap = importlib.import_module('discord_voice.bootstrap')
    calls = []
    ready = {'python': None, 'opus': None, 'ffmpeg': None}
    assert bootstrap.bootstrap(ready, apply=True, system=True, python_deps=True,
                               runner=calls.append, probe=lambda: ready) == ready
    assert calls == []


def test_install_requires_explicit_scope_and_admin():
    b = importlib.import_module('discord_voice.bootstrap')
    checks = {'python': None, 'opus': 'missing', 'ffmpeg': None}
    calls = []
    for options, text in [({}, 'system'), ({'system': True, 'uid': 10000}, 'administrator')]:
        try:
            b.bootstrap(checks, apply=True, runner=calls.append, distro='debian', **options)
        except b.BootstrapError as exc:
            assert text in str(exc).lower()
        else:
            assert False, 'Missing permission must refuse'
    assert calls == []


def test_dry_run_is_read_only_and_apply_reprobes():
    b = importlib.import_module('discord_voice.bootstrap')
    checks = {'python': None, 'opus': 'missing', 'ffmpeg': None}
    ready = dict.fromkeys(checks)
    calls = []
    assert b.bootstrap(checks, system=True, distro='debian', uid=0,
                       runner=calls.append) == checks
    assert calls == []
    assert b.bootstrap(checks, apply=True, system=True, distro='debian', uid=0,
                       runner=calls.append, probe=lambda: ready) == ready
    assert calls == [['apt-get', 'install', '-y', 'libopus0']]
    b.bootstrap(ready, apply=True, system=True, runner=calls.append, probe=lambda: ready)
    assert len(calls) == 1
    try:
        b.bootstrap(checks, apply=True, system=True, distro='debian', uid=0,
                    runner=calls.append, probe=lambda: checks)
    except b.BootstrapError as exc:
        assert 'verification' in str(exc).lower()
    else:
        assert False, 'Successful package command alone is not readiness'


def test_preflight_real_local_codec_and_ffmpeg():
    b = importlib.import_module('discord_voice.bootstrap')
    checks = b.preflight()
    assert checks == {'python': None, 'opus': None, 'ffmpeg': None}, checks
    assert b.detect_distro() in {'debian', 'ubuntu', 'fedora', 'rhel', 'alpine', 'arch'}


def test_preflight_missing_ffmpeg_is_actionable():
    from unittest.mock import patch
    b = importlib.import_module('discord_voice.bootstrap')
    with patch('shutil.which', return_value=None):
        assert 'ffmpeg' in b.preflight()['ffmpeg'].lower()


def test_python_install_is_explicit_and_uses_current_interpreter():
    import sys
    b = importlib.import_module('discord_voice.bootstrap')
    missing = {'python': 'missing', 'opus': None, 'ffmpeg': None}
    calls = []
    try:
        b.bootstrap(missing, apply=True, runner=calls.append, probe=lambda: dict.fromkeys(missing))
    except b.BootstrapError as exc:
        assert '--python-deps' in str(exc)
    else:
        assert False, 'Python install requires explicit scope'
    b.bootstrap(missing, apply=True, python_deps=True, runner=calls.append,
                probe=lambda: dict.fromkeys(missing))
    assert calls[0][:4] == [sys.executable, '-m', 'pip', 'install']
    from pathlib import Path
    assert calls[0][4] == str(Path(b.__file__).resolve().parent.parent)


def test_cli_dry_run_does_not_claim_readiness():
    import contextlib
    import io
    from unittest.mock import patch
    b = importlib.import_module('discord_voice.bootstrap')
    output = io.StringIO()
    with patch.object(b, 'preflight', return_value={'python': None, 'opus': 'missing', 'ffmpeg': None}), contextlib.redirect_stdout(output):
        assert b.main(['--dry-run', '--system']) == 1
    assert 'libopus0' in output.getvalue()
    assert 'not ready' in output.getvalue().lower()


def test_python_install_refuses_system_interpreter():
    import sys
    from unittest.mock import patch
    b = importlib.import_module('discord_voice.bootstrap')
    calls = []
    with patch.object(sys, 'prefix', sys.base_prefix):
        try:
            b.bootstrap({'python': 'missing', 'opus': None, 'ffmpeg': None},
                        apply=True, python_deps=True, runner=calls.append)
        except b.BootstrapError as exc:
            assert 'virtual environment' in str(exc)
        else:
            assert False, 'Never mutate the system interpreter'
    assert calls == []


def test_linux_plans_only_missing_packages():
    b = importlib.import_module('discord_voice.bootstrap')
    assert b.system_command({'opus': 'missing', 'ffmpeg': None}, 'debian') == ['apt-get', 'install', '-y', 'libopus0']
    assert b.system_command({'opus': 'missing', 'ffmpeg': 'missing'}, 'alpine') == ['apk', 'add', 'opus', 'ffmpeg']
    assert b.system_command({'opus': 'missing', 'ffmpeg': None}, 'fedora') == ['dnf', 'install', '-y', 'opus']
    assert b.system_command({'opus': 'missing', 'ffmpeg': None}, 'arch') == ['pacman', '-S', '--needed', '--noconfirm', 'opus']
    try:
        b.system_command({'opus': 'missing'}, 'unknown')
    except b.BootstrapError as exc:
        assert 'unsupported' in str(exc).lower()
    else:
        assert False, 'Unknown distro must refuse rather than guess'
