"""Shared bot-loading utility for runner scripts.

If a bot ships a ``bot_loader.py`` in its root directory, that file is
loaded and used to create the bot instance.  This avoids the
``importlib.import_module`` approach in run_vs_computer / run_from_replay
which can cause module-name collisions (e.g. the runner's ``config.py``
shadowing a bot's own ``config`` module).

A ``bot_loader.py`` is a tiny per-bot file with three things::

    from my_package.my_module import MyBot

    BOT_NAME = "MyBot"
    BOT_DEFAULT_RACE = "Terran"   # or Random, Protoss, Zerg

    def create_bot():
        return MyBot()
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys

logger = logging.getLogger(__name__)


def try_load_bot_loader(bot_dir: str):
    """Try to load ``bot_loader.py`` from *bot_dir*.

    If found, adjusts ``sys.path`` and ``sys.modules`` so the bot's own
    modules take precedence over runner modules during import, then loads
    and returns the module.  Returns ``None`` if the file doesn't exist.
    """
    loader_path = os.path.join(bot_dir, "bot_loader.py")
    if not os.path.isfile(loader_path):
        return None

    # The runner's ``from config import ...`` (at module level) caches the
    # runner's config.py in sys.modules['config'].  Evict it so the bot's
    # own config.py (if any) is found when the bot's import chain resolves.
    sys.modules.pop("config", None)

    # Put bot dir first on sys.path so bot-local modules take precedence
    # over runner modules (e.g. /root/runner) during the import chain.
    if bot_dir in sys.path:
        sys.path.remove(bot_dir)
    sys.path.insert(0, bot_dir)

    spec = importlib.util.spec_from_file_location("bot_loader", loader_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    logger.info(
        "Loaded bot via bot_loader.py: %s (default_race=%s)",
        getattr(mod, "BOT_NAME", "?"),
        getattr(mod, "BOT_DEFAULT_RACE", "?"),
    )
    return mod
