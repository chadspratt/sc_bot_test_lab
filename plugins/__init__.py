"""Plugin discovery for the Custom page.

Each plugin module in this directory must define:
    name: str          — display name shown on the Custom page
    description: str   — short help text shown below the button/tab

    For action plugins (buttons on the "Other" tab):
        def execute(request) -> str  — performs the action, returns a status message
            (raise an exception to signal failure; the message is shown on success)

    For fullpage plugins (displayed as their own subtab):
        fullpage: bool = True
        def get_context(request) -> dict  — returns template context
        template: str  — path to the Django template (e.g. 'test_lab/plugins/my_plugin.html')

Usage::

    from test_lab.plugins import discover_plugins, get_plugin

    plugins = discover_plugins()        # list of plugin modules, sorted by name
    plugin = get_plugin('my_plugin')    # fetch one by module-filename stem
"""

import importlib
import logging
import os
from types import ModuleType
from typing import Optional

logger = logging.getLogger(__name__)

_PLUGINS_DIR = os.path.dirname(__file__)


def discover_plugins() -> list[ModuleType]:
    """Scan this directory and return all valid plugin modules, sorted by name."""
    plugins = []
    for filename in sorted(os.listdir(_PLUGINS_DIR)):
        if filename.startswith('_') or not filename.endswith('.py'):
            continue
        module_name = filename[:-3]
        try:
            module = importlib.import_module(f'test_lab.plugins.{module_name}')
            _validate_plugin(module, module_name)
            plugins.append(module)
        except Exception:
            logger.exception('Failed to load plugin %r', module_name)
    return plugins


def get_plugin(plugin_name: str) -> Optional[ModuleType]:
    """Return the plugin module for *plugin_name* (filename stem), or None."""
    if not _is_safe_plugin_name(plugin_name):
        return None
    try:
        module = importlib.import_module(f'test_lab.plugins.{plugin_name}')
        _validate_plugin(module, plugin_name)
        return module
    except Exception:
        logger.exception('Failed to load plugin %r', plugin_name)
        return None


def is_fullpage(module: ModuleType) -> bool:
    """Return True if the plugin occupies a full subtab."""
    return getattr(module, 'fullpage', False)


def _validate_plugin(module: ModuleType, name: str) -> None:
    required_common = ('name', 'description')
    for attr in required_common:
        if not hasattr(module, attr):
            raise AttributeError(f"Plugin {name!r} is missing required attribute {attr!r}")

    if is_fullpage(module):
        for attr in ('get_context', 'template'):
            if not hasattr(module, attr):
                raise AttributeError(f"Fullpage plugin {name!r} is missing required attribute {attr!r}")
        if not callable(module.get_context):
            raise TypeError(f"Plugin {name!r}: get_context must be callable")
    else:
        if not hasattr(module, 'execute'):
            raise AttributeError(f"Plugin {name!r} is missing required attribute 'execute'")
        if not callable(module.execute):
            raise TypeError(f"Plugin {name!r}: execute must be callable")


def _is_safe_plugin_name(name: str) -> bool:
    """Guard against path traversal — plugin names must be simple identifiers."""
    return name.isidentifier() and not name.startswith('_')
