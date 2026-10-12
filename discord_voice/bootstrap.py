"""Explicit, offline-safe voice dependency bootstrap (no account/config access)."""


class BootstrapError(RuntimeError):
    """An actionable prerequisite or installation failure."""


def system_command(checks, distro):
    families = {
        'debian': (['apt-get', 'install', '-y'], 'libopus0'),
        'ubuntu': (['apt-get', 'install', '-y'], 'libopus0'),
        'fedora': (['dnf', 'install', '-y'], 'opus'),
        'rhel': (['dnf', 'install', '-y'], 'opus'),
        'alpine': (['apk', 'add'], 'opus'),
        'arch': (['pacman', '-S', '--needed', '--noconfirm'], 'opus'),
    }
    if distro not in families:
        raise BootstrapError(f'Unsupported distro {distro!r}; install libopus and ffmpeg with the host administrator.')
    command, opus = families[distro]
    packages = ([opus] if checks.get('opus') else []) + (['ffmpeg'] if checks.get('ffmpeg') else [])
    return command + packages if packages else []


def detect_distro():
    import platform
    if platform.system() != 'Linux':
        raise BootstrapError('System bootstrap supports Linux only; on macOS use brew install opus ffmpeg; on Windows use Discord.py bundled Opus and install ffmpeg on PATH.')
    info = platform.freedesktop_os_release()
    for candidate in [info.get('ID', ''), *info.get('ID_LIKE', '').split()]:
        if candidate in {'debian', 'ubuntu', 'fedora', 'rhel', 'alpine', 'arch'}:
            return candidate
    raise BootstrapError('Unsupported Linux distribution; ask the host administrator to install libopus and ffmpeg.')


def preflight():
    """Local import/version, Opus encode/decode and ffmpeg executable probes only."""
    import importlib
    import importlib.metadata
    import shutil
    import subprocess
    checks = dict.fromkeys(['python', 'opus', 'ffmpeg'])
    try:
        for name, module, expected in [('discord.py', 'discord', '2.7.1'),
                                       ('aiortc', 'aiortc', '1.15.0'),
                                       ('av', 'av', '17.1.0'),
                                       ('aiohttp', 'aiohttp', None),
                                       ('PyNaCl', 'nacl', None)]:
            version = importlib.metadata.version(name)
            if expected and version != expected:
                raise RuntimeError(f'{name} {expected} required; found {version}')
            if name == 'aiohttp' and not (version.startswith('3.') and int(version.split('.')[1]) >= 13):
                raise RuntimeError(f'aiohttp >=3.13,<4 required; found {version}')
            importlib.import_module(module)
    except Exception as exc:
        checks['python'] = f'Python voice dependencies unavailable: {exc}; install this checkout in the owning host interpreter.'
    try:
        from discord import opus
        packet = opus.Encoder().encode(bytes(3840), 960)
        pcm = opus.Decoder().decode(packet)
        if not packet or len(pcm) != 3840:
            raise RuntimeError('20ms stereo 48kHz roundtrip returned invalid PCM')
    except Exception as exc:
        checks['opus'] = f'Native libopus encode/decode unavailable: {type(exc).__name__}: {exc}; Linux Debian/Ubuntu needs libopus0 (not an LLM model).'
    try:
        executable = shutil.which('ffmpeg')
        if not executable:
            raise RuntimeError('ffmpeg missing from PATH')
        subprocess.run([executable, '-version'], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except Exception as exc:
        checks['ffmpeg'] = f'ffmpeg executable unavailable: {exc}; install ffmpeg on the owning host.'
    return checks


def bootstrap(checks, *, apply=False, system=False, python_deps=False,
              runner=None, probe=None, distro=None, uid=None):
    import os
    import subprocess
    if not apply or not any(checks.values()):
        return checks
    run = runner or (lambda argv: subprocess.run(argv, check=True, timeout=600))
    commands = []
    if checks.get('python'):
        import sys
        from pathlib import Path
        if not python_deps:
            raise BootstrapError('Missing Python dependencies; for Hermes use PM dependency consent; for a standalone test virtual environment explicitly authorize --python-deps.')
        if sys.prefix == sys.base_prefix:
            raise BootstrapError('--python-deps requires a standalone virtual environment; never mutate a system interpreter. Hermes environments must use PM admission.')
        source = Path(__file__).resolve().parent.parent
        if not (source / 'pyproject.toml').is_file():
            raise BootstrapError('Run --python-deps from the reviewed source checkout, not a wheel installation.')
        commands.append([sys.executable, '-m', 'pip', 'install', str(source)])
    if checks.get('opus') or checks.get('ffmpeg'):
        if not system:
            raise BootstrapError('Missing native dependencies; explicitly authorize --system or ask the host administrator.')
        command = system_command(checks, distro or detect_distro())
        if (os.geteuid() if uid is None else uid) != 0:
            raise BootstrapError('Host administrator required: ' + ' '.join(command) + '; no sudo or privilege escalation is attempted.')
        commands.insert(0, command)
    try:
        for command in commands:
            run(command)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BootstrapError(f'Package command failed: {exc}; verify pip exists in the selected interpreter and the host package repositories are available.') from exc
    result = (probe or preflight)()
    if any(result.values()):
        raise BootstrapError(f'Post-install verification failed: {result}')
    return result


def main(argv=None):
    import argparse
    import shlex
    import sys
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='explicitly execute selected missing-dependency installs')
    mode.add_argument('--dry-run', action='store_true', help='print plan only (the default)')
    parser.add_argument('--system', action='store_true', help='authorize native packages; apply requires administrator/root')
    parser.add_argument('--python-deps', action='store_true', help='standalone virtual environments only: authorize pip install of this source checkout (Hermes uses PM)')
    args = parser.parse_args(argv)
    checks = preflight()
    for name, error in checks.items():
        print(f'{name}: {error or "ready"}')
    try:
        if checks['opus'] or checks['ffmpeg']:
            print('Administrator plan: ' + shlex.join(system_command(checks, detect_distro())))
        if checks['python']:
            print('Python plan: ' + shlex.join([sys.executable, '-m', 'pip', 'install', str(Path(__file__).resolve().parent.parent)]))
        result = bootstrap(checks, apply=args.apply, system=args.system, python_deps=args.python_deps)
    except BootstrapError as exc:
        print(f'Not ready: {exc}')
        return 1
    ready = not any(result.values())
    print('Local voice dependencies ready (not a live voice/account test).' if ready else 'Not ready; dry-run made no changes. Select explicit install scopes or ask the host administrator.')
    return 0 if ready else 1


if __name__ == '__main__':
    raise SystemExit(main())
