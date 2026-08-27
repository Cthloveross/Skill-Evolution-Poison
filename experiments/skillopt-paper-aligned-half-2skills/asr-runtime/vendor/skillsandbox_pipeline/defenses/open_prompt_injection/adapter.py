from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


def _import_module_from_file(name: str, file_path: Path):
    """Import a single .py file as a module without triggering its package __init__."""
    spec = importlib.util.spec_from_file_location(name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class BackendUnavailableError(RuntimeError):
    """Raised when an upstream Open-Prompt-Injection backend cannot be used."""


class OpenPromptInjectionAdapter:
    """Load upstream DataSentinel and PromptLocate only when explicitly configured."""

    def __init__(self):
        repo_root = Path(__file__).resolve().parent.parent.parent
        self.opi_root = repo_root / "Open-Prompt-Injection"
        default_config = self.opi_root / "configs" / "model_configs" / "mistral_config.json"

        self.datasentinel_ft_path = os.environ.get("DATASENTINEL_FT_PATH", "").strip()
        self.datasentinel_config_path = Path(
            os.environ.get("DATASENTINEL_CONFIG_PATH", str(default_config))
        )
        self.datasentinel_device = os.environ.get("DATASENTINEL_DEVICE", "").strip()

        self.promptlocate_ft_path = os.environ.get("PROMPTLOCATE_FT_PATH", "").strip()
        self.promptlocate_config_path = Path(
            os.environ.get("PROMPTLOCATE_CONFIG_PATH", str(default_config))
        )
        self.promptlocate_device = os.environ.get("PROMPTLOCATE_DEVICE", "").strip()

        self._datasentinel = None
        self._promptlocate = None

    def datasentinel_available(self) -> tuple[bool, str | None]:
        if not self.datasentinel_ft_path:
            return False, "DATASENTINEL_FT_PATH is not configured."
        if not self.opi_root.exists():
            return False, f"Open-Prompt-Injection directory not found: {self.opi_root}"
        if not self.datasentinel_config_path.exists():
            return False, f"DataSentinel config path not found: {self.datasentinel_config_path}"
        return True, None

    def promptlocate_available(self) -> tuple[bool, str | None]:
        if not self.promptlocate_ft_path:
            return False, "PROMPTLOCATE_FT_PATH is not configured."
        if not self.opi_root.exists():
            return False, f"Open-Prompt-Injection directory not found: {self.opi_root}"
        if not self.promptlocate_config_path.exists():
            return False, f"PromptLocate config path not found: {self.promptlocate_config_path}"
        return True, None

    def detect(self, text: str) -> bool:
        detector = self.load_datasentinel()
        return bool(detector.detect(text))

    def locate_and_recover(self, candidate_text: str, target_instruction: str) -> tuple[str, str]:
        locator = self.load_promptlocate()
        return locator.locate_and_recover(candidate_text, target_instruction)

    def _ensure_opi_submodules(self):
        """Register the minimal OPI submodules we need, bypassing the heavy top-level __init__.

        We create stub package entries so that relative imports inside
        DataSentinelDetector.py (``from ..models import QLoraModel``) resolve
        without importing the full OpenPromptInjection package (which pulls
        in Vicuna, PaLM2, and other providers with unneeded dependencies).
        """
        if "OpenPromptInjection.models.QLoraModel" in sys.modules:
            return  # already registered

        import types

        opi_pkg = self.opi_root / "OpenPromptInjection"

        # Stub top-level package so relative imports resolve
        if "OpenPromptInjection" not in sys.modules:
            pkg = types.ModuleType("OpenPromptInjection")
            pkg.__path__ = [str(opi_pkg)]
            pkg.__package__ = "OpenPromptInjection"
            sys.modules["OpenPromptInjection"] = pkg

        # Stub models sub-package
        if "OpenPromptInjection.models" not in sys.modules:
            models_pkg = types.ModuleType("OpenPromptInjection.models")
            models_pkg.__path__ = [str(opi_pkg / "models")]
            models_pkg.__package__ = "OpenPromptInjection.models"
            sys.modules["OpenPromptInjection.models"] = models_pkg

        # Stub apps sub-package
        if "OpenPromptInjection.apps" not in sys.modules:
            apps_pkg = types.ModuleType("OpenPromptInjection.apps")
            apps_pkg.__path__ = [str(opi_pkg / "apps")]
            apps_pkg.__package__ = "OpenPromptInjection.apps"
            sys.modules["OpenPromptInjection.apps"] = apps_pkg

        # Model base class
        model_mod = _import_module_from_file(
            "OpenPromptInjection.models.Model",
            opi_pkg / "models" / "Model.py",
        )
        sys.modules["OpenPromptInjection.models"].Model = model_mod.Model

        # QLoraModel
        qlora_mod = _import_module_from_file(
            "OpenPromptInjection.models.QLoraModel",
            opi_pkg / "models" / "QLoraModel.py",
        )
        sys.modules["OpenPromptInjection.models"].QLoraModel = qlora_mod.QLoraModel

    def load_datasentinel(self):
        if self._datasentinel is not None:
            return self._datasentinel

        available, reason = self.datasentinel_available()
        if not available:
            raise BackendUnavailableError(reason or "DataSentinel backend unavailable.")

        try:
            self._ensure_opi_submodules()

            ds_mod = _import_module_from_file(
                "OpenPromptInjection.apps.DataSentinelDetector",
                self.opi_root / "OpenPromptInjection" / "apps" / "DataSentinelDetector.py",
            )
            DataSentinelDetector = ds_mod.DataSentinelDetector

            config = _load_json_config(self.datasentinel_config_path)
            config.setdefault("params", {})
            config["params"]["ft_path"] = self.datasentinel_ft_path
            if self.datasentinel_device:
                config["params"]["device"] = self.datasentinel_device
            self._datasentinel = DataSentinelDetector(config)
            return self._datasentinel
        except Exception as exc:  # pragma: no cover - depends on external checkpoints/runtime
            raise BackendUnavailableError(f"Failed to initialize DataSentinel: {exc}") from exc

    def load_promptlocate(self):
        if self._promptlocate is not None:
            return self._promptlocate

        available, reason = self.promptlocate_available()
        if not available:
            raise BackendUnavailableError(reason or "PromptLocate backend unavailable.")

        try:
            self._ensure_opi_submodules()

            pl_mod = _import_module_from_file(
                "OpenPromptInjection.apps.PromptLocate",
                self.opi_root / "OpenPromptInjection" / "apps" / "PromptLocate.py",
            )
            PromptLocate = pl_mod.PromptLocate

            config = _load_json_config(self.promptlocate_config_path)
            config.setdefault("params", {})
            config["params"]["ft_path"] = self.promptlocate_ft_path
            if self.promptlocate_device:
                config["params"]["device"] = self.promptlocate_device
            self._promptlocate = PromptLocate(config)
            return self._promptlocate
        except Exception as exc:  # pragma: no cover - depends on external checkpoints/runtime
            raise BackendUnavailableError(f"Failed to initialize PromptLocate: {exc}") from exc


def _load_json_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
