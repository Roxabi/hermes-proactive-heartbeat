from __future__ import annotations

import importlib.util
import io
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

from tests.isolation import IsolatedHomeTestCase

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "hermes_proactive_heartbeats"


class FakeState:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self.gets: list[tuple[str, Any]] = []
        self.sets: list[tuple[str, Any]] = []

    def get(self, key: str, default: Any = None) -> Any:
        self.gets.append((key, default))
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.sets.append((key, value))
        self._data[key] = value


class FakeCtx:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.state = FakeState()
        self.commands: list[dict[str, Any]] = []

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def register_cli_command(self, **kwargs: Any) -> None:
        self.commands.append(kwargs)


def _load_module(module_name: str, path: Path, package: ModuleType | None = None) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        module_name,
        path,
        submodule_search_locations=[str(path.parent)] if path.name == "__init__.py" else None,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    if package is not None:
        setattr(package, module_name.rsplit(".", 1)[-1], module)
    spec.loader.exec_module(module)
    return module


def load_plugin_package() -> ModuleType | None:
    existing = sys.modules.get(PACKAGE_NAME)
    if existing is not None and hasattr(existing, "register"):
        return existing

    init_path = ROOT / "__init__.py"
    if not init_path.exists():
        return None

    package = ModuleType(PACKAGE_NAME)
    package.__file__ = str(init_path)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    package.__package__ = PACKAGE_NAME
    sys.modules[PACKAGE_NAME] = package

    for path in sorted(ROOT.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module_name = f"{PACKAGE_NAME}.{path.stem}"
        module = _load_module(module_name, path, package)
        sys.modules.setdefault(path.stem, module)

    use_cases_dir = ROOT / "use_cases"
    if use_cases_dir.is_dir():
        use_cases_pkg = ModuleType(f"{PACKAGE_NAME}.use_cases")
        use_cases_pkg.__path__ = [str(use_cases_dir)]  # type: ignore[attr-defined]
        use_cases_pkg.__package__ = f"{PACKAGE_NAME}.use_cases"
        sys.modules[f"{PACKAGE_NAME}.use_cases"] = use_cases_pkg
        sys.modules.setdefault("use_cases", use_cases_pkg)
        package.use_cases = use_cases_pkg
        for path in sorted(use_cases_dir.glob("*.py")):
            if path.name == "__init__.py":
                module = _load_module(f"{PACKAGE_NAME}.use_cases", path)
                sys.modules[f"{PACKAGE_NAME}.use_cases"] = module
                sys.modules.setdefault("use_cases", module)
                package.use_cases = module
                continue
            module_name = f"{PACKAGE_NAME}.use_cases.{path.stem}"
            module = _load_module(module_name, path, use_cases_pkg)
            sys.modules.setdefault(f"use_cases.{path.stem}", module)

    return _load_module(PACKAGE_NAME, init_path)


def load_register() -> Callable[[Any], Any] | None:
    try:
        package = load_plugin_package()
    except Exception:
        return None
    if package is None:
        return None
    register = getattr(package, "register", None)
    return register if callable(register) else None


class PluginRegistrationTests(IsolatedHomeTestCase):
    def test_register_only_registers_cli_command_without_setup_side_effects(self) -> None:
        register = load_register()
        if register is None:
            self.skipTest("plugin register() is not importable without a Hermes install")

        ctx = FakeCtx()
        with (
            mock.patch("subprocess.run") as subprocess_run,
            mock.patch("subprocess.Popen") as subprocess_popen,
            mock.patch("pathlib.Path.mkdir") as path_mkdir,
            mock.patch("builtins.open", side_effect=AssertionError("open during register")),
        ):
            register(ctx)

        self.assertEqual(len(ctx.commands), 1)
        command = ctx.commands[0]
        self.assertEqual(command["name"], "proactive-heartbeats")
        self.assertTrue(callable(command["setup_fn"]))
        self.assertTrue(callable(command["handler_fn"]))
        self.assertEqual(ctx.state.sets, [])
        self.assertEqual(ctx.state.gets, [])
        subprocess_run.assert_not_called()
        subprocess_popen.assert_not_called()
        path_mkdir.assert_not_called()


class PluginTickHandlerTests(IsolatedHomeTestCase):
    def test_tick_handler_loads_and_persists_engine_state_then_prints_render(self) -> None:
        register = load_register()
        if register is None:
            self.skipTest("plugin register() is not importable without a Hermes install")

        previous_state = {
            "version": 1,
            "use_cases": {},
            "delivered": {},
        }
        next_state = {
            "version": 1,
            "use_cases": {"host": {"state": {}, "active": []}},
            "delivered": {},
        }
        rendered = '{"wakeAgent": false}'
        tick_result = SimpleNamespace(state=next_state, diagnostics={}, render=lambda: rendered)
        engine = SimpleNamespace(tick=mock.Mock(return_value=tick_result))

        root = self.hermes_home / "proactive-heartbeats"
        (root / "heartbeats").mkdir(parents=True)
        (root / "proactive-heartbeats.json").write_text(
            '{"heartbeats": ["care"]}', encoding="utf-8"
        )
        (root / "heartbeats" / "care.json").write_text(
            '{"delivery": {"target": "origin"}, "collectors": {}}', encoding="utf-8"
        )

        ctx = FakeCtx()
        ctx.state.set("heartbeat:care", previous_state)
        ctx.state.sets.clear()
        ctx.state.gets.clear()

        register(ctx)
        handler = ctx.commands[0]["handler_fn"]
        args = SimpleNamespace(proactive_heartbeats_command="tick", name="care")
        stdout = io.StringIO()
        cli_module = sys.modules[f"{PACKAGE_NAME}.cli"]
        build_registry = mock.Mock(return_value=[])
        engine_cls = mock.Mock(return_value=engine)
        typesafe_cls = mock.Mock(return_value=object())

        with (
            mock.patch.object(
                cli_module,
                "_import_tick_deps",
                return_value=(engine_cls, build_registry, typesafe_cls),
            ),
            mock.patch.object(sys, "stdout", stdout),
        ):
            exit_code = handler(args)

        self.assertEqual(exit_code, 0)
        engine_cls.assert_called_once()
        engine.tick.assert_called_once()
        self.assertEqual(engine.tick.call_args.kwargs["previous_state"], previous_state)
        self.assertIn(("heartbeat:care", None), ctx.state.gets)
        self.assertEqual(ctx.state.sets, [("heartbeat:care", next_state)])
        self.assertEqual(stdout.getvalue(), rendered + "\n")
