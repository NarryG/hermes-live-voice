"""English error-message regressions; no CLI, credentials, audio or network."""
import pytest
from test_plugin_api import PLUGIN_API, load_unit


def test_missing_codex_message_is_english():
    ns = load_unit(PLUGIN_API, '_cl_ensure')
    ns.update(_CL={'proc': None}, _codex_binary=lambda: None)
    with pytest.raises(RuntimeError, match='Codex CLI was not found'):
        ns['_cl_ensure']()
