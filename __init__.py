"""English-first Live Voice: Desktop/dashboard and scoped native Discord lane."""
from __future__ import annotations
import logging
import threading

_log = logging.getLogger('hermes.plugins.talk-desktop')


def register(ctx) -> None:
    """Wire only the existing native Discord bot; no startup audio or providers."""
    from hermes_constants import get_hermes_home
    home = get_hermes_home().resolve()  # immutable registration scope, not a late ambient read
    from .discord_voice.owner import capture_selection
    selected_capture = capture_selection(home)  # environment/CWD only; no auth/config read
    handles = []
    unloaded = threading.Event()

    def discord_factory(native, adapter):
        if unloaded.is_set():
            return
        from .discord_voice.native import defer_install
        # Defer all owner-file reads until the gateway publishes this exact
        # adapter; ambient Desktop selection is never ownership evidence.
        handle = defer_install(native, adapter, home=home, selected_capture=selected_capture)
        handles.append(handle)
        # Unload may run on another thread while the factory creates the handle.
        if unloaded.is_set():
            handle.shutdown()

    def unload():
        unloaded.set()
        for handle in handles:
            handle.shutdown()
        # Retain handles for pending cleanup; the host's synchronous hook cannot
        # promise that a Discord HTTP deletion/disconnect has completed.
        _log.warning('Live Voice unloaded: owning gateway restart required before re-enable/reload; native factory keys are cached for the bot lifetime.')

    ctx.register_platform_handler('discord', discord_factory)
    ctx.on_unload(unload)
    _log.info('Live Voice: Desktop API and private native Discord commands registered')
