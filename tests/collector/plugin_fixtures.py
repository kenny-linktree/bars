"""The standard plugins as the tests see them: discovered from plugins/ like the collector does."""
from pathlib import Path

from bars_collector import plugins
from bars_collector.runner import run

ROOT = Path(__file__).resolve().parents[2]
STANDARD_DIR = ROOT / "plugins"
STANDARD = plugins.discover(STANDARD_DIR)
STANDARD_IDS = [plugin.id for plugin in STANDARD]
_modules = {}


def plugin(pid):
    """The plugin module itself, for exercising `parse`, `collect` and `detect` in-process."""
    if pid not in _modules:
        _modules[pid] = plugins.load_module(pid, STANDARD_DIR / pid / plugins.MODULE_NAME)
    return _modules[pid]


def parse(pid):
    return plugin(pid).parse


def run_standard(output, selected, **kwargs):
    """`runner.run` against the standard plugins, the way `__main__` calls it."""
    kwargs.setdefault("plugins", STANDARD)
    return run(output, selected, **kwargs)


def write_plugin(root, pid, manifest=None, module=None, name=None):
    """Create a synthetic plugin directory. `module` is plugin.py source; None writes none."""
    directory = Path(root) / pid
    directory.mkdir(parents=True, exist_ok=True)
    if manifest is None:
        manifest = {"manifest_version": 1, "id": pid, "name": name or pid.capitalize()}
    if manifest is not False:
        import json
        (directory / plugins.MANIFEST_NAME).write_text(json.dumps(manifest) if not isinstance(manifest, str) else manifest)
    if module is not None:
        (directory / plugins.MODULE_NAME).write_text(module)
    return directory
