"""Offline test safety: only the explicitly labelled fixture child may run.

This prevents an admission-regression RED from reaching the real Codex binary.
"""
import asyncio
from pathlib import Path
import sys
import pytest


@pytest.fixture(autouse=True)
def forbid_real_voice_subprocesses(monkeypatch):
    original = asyncio.create_subprocess_exec
    async def safe_exec(*argv, **kwargs):
        assert argv[0] == sys.executable, 'No real provider processes in tests'
        allowed = {Path(__file__).parent / 'fixtures' / 'fake_cli_metadata.py'}
        assert Path(argv[1]).resolve() in allowed, 'Only fixed labelled stdio fixtures may run'
        assert 'CODEX_HOME' in kwargs.get('env', {})
        return await original(*argv, **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', safe_exec)
