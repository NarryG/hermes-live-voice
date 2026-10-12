import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import sys
import pytest


@pytest.fixture(autouse=True)
def fixture_registration_home(tmp_path, monkeypatch):
    from types import ModuleType
    constants = ModuleType('hermes_constants')
    constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, 'hermes_constants', constants)


def test_unload_announces_gateway_restart_and_fences_retained_factory(caplog, monkeypatch):
    import logging
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('lifecycle_fixture', root / '__init__.py', submodule_search_locations=[str(root)])
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)  # real package import semantics
    spec.loader.exec_module(plugin)
    factories, cleanup = [], []
    plugin.register(NS(register_platform_handler=lambda p,f: factories.append(f), on_unload=cleanup.append))
    with caplog.at_level(logging.WARNING):
        cleanup[0]()
    assert 'gateway restart' in caplog.text.lower()
    # Even a retained old factory cannot re-open after this registration unloads.
    factories[0](object(), object())


def test_plugin_registers_native_factory_and_unload_without_importing_discord(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('livevoice_plugin_fixture', root / '__init__.py', submodule_search_locations=[str(root)])
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)  # real package import semantics
    spec.loader.exec_module(plugin)
    factories, cleanup = [], []
    ctx = NS(register_platform_handler=lambda platform, factory: factories.append((platform, factory)),
             on_unload=cleanup.append)
    plugin.register(ctx)
    assert len(factories) == 1 and factories[0][0] == 'discord'
    assert cleanup
