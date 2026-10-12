from pathlib import Path


def test_ci_installs_discord_for_native_voice_regressions():
    source = (Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml').read_text()
    assert 'discord.py==2.7.1' in source, 'The native voice regressions require the tested Discord SDK in CI'


def test_release_has_no_unsafe_websocket_executor_prototype():
    root = Path(__file__).resolve().parents[1]
    assert not (root / 'discord_voice/codex.py').exists()
    assert not (root / 'tests/fixtures/fake_codex_stdio.py').exists()


def test_dependency_manifest_ci_and_licensing_agree():
    import tomllib
    import yaml
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root/'pyproject.toml').read_text())['project']
    manifest = yaml.safe_load((root/'plugin.yaml').read_text())
    assert project['version'] == manifest['version']
    assert {'aiortc==1.15.0','av==17.1.0','aiohttp>=3.13,<4','discord.py[voice]==2.7.1'} <= set(project['dependencies'])
    assert 'test' in project['optional-dependencies']
    ci = (root/'.github/workflows/ci.yml').read_text()
    assert "'.[test]'" in ci
    assert (root/'licenses/Codex-Apache-2.0.txt').is_file()
    assert '092d3acd6bec3e3a14bdc7e7a2810ab628ab759d' in (root/'NOTICE.md').read_text()
