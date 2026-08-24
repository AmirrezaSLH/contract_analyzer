"""The prompt library: every instruction the model is given, in one JSON file.

Prompts are data, not code. Keeping them in `prompts.json` means the demo can
show exactly what the model was told, a reviewer can diff a wording change
without reading Python, and `PROMPTS_PATH` can point the same package at a
different domain. The file is read once and validated on load: a missing key
fails at startup with the file's path and the keys it does have, not halfway
through the first analysis with a `KeyError: 'analysis.system'`.

Format: `{"version": 1, "prompts": {"agent.system": "...", ...}}`, flat.
Placeholders are `str.format` fields, and `format()` reports a missing one by
name.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ..config import Settings, get_settings

SUPPORTED_VERSION = 1

#: Every key generation needs. Checked on load so the failure is early and
#: names the file. The Evaluator's own keys are added beside these.
REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "agent.system",
        "chat.system",
        "chat.no_context",
        "analysis.system",
        "analysis.user",
        "analysis.finish",
        "analysis.fix_structure",
        "analysis.revise",
        "evaluator.system",
        "evaluator.user",
    }
)


class PromptError(RuntimeError):
    """The prompt file is missing, malformed, or lacks a key the code needs."""


class PromptLibrary:
    def __init__(self, prompts: dict[str, str], *, path: Path | None = None) -> None:
        self._prompts = dict(prompts)
        self.path = path

    @classmethod
    def load(cls, path: Path) -> PromptLibrary:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise PromptError(f"prompt file not found: {path}") from None
        except json.JSONDecodeError as exc:
            raise PromptError(f"prompt file {path} is not valid JSON: {exc}") from None
        if not isinstance(raw, dict) or raw.get("version") != SUPPORTED_VERSION:
            raise PromptError(
                f"prompt file {path}: expected {{\"version\": {SUPPORTED_VERSION}, ...}}"
            )
        prompts = raw.get("prompts")
        if not isinstance(prompts, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in prompts.items()
        ):
            raise PromptError(f"prompt file {path}: `prompts` must map names to strings")
        missing = sorted(REQUIRED_KEYS - prompts.keys())
        if missing:
            raise PromptError(
                f"prompt file {path} lacks {', '.join(missing)}; "
                f"it has {', '.join(sorted(prompts))}"
            )
        return cls(prompts, path=path)

    def get(self, name: str) -> str:
        try:
            return self._prompts[name]
        except KeyError:
            raise PromptError(
                f"no prompt named {name!r} in {self.path or 'the prompt library'}; "
                f"it has {', '.join(sorted(self._prompts))}"
            ) from None

    def format(self, name: str, **fields: object) -> str:
        try:
            return self.get(name).format(**fields)
        except KeyError as exc:
            raise PromptError(f"prompt {name!r} needs a value for {exc.args[0]!r}") from None

    def __contains__(self, name: object) -> bool:
        return name in self._prompts

    def keys(self) -> list[str]:
        return sorted(self._prompts)


@lru_cache(maxsize=4)
def _load(path: Path) -> PromptLibrary:
    return PromptLibrary.load(path)


def get_prompts(settings: Settings | None = None) -> PromptLibrary:
    """The library at `settings.prompts_path`, loaded once per path."""
    settings = settings or get_settings()
    return _load(Path(settings.prompts_path))


__all__ = ["REQUIRED_KEYS", "PromptError", "PromptLibrary", "get_prompts"]
