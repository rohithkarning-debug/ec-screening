"""Versioned prompts (L2).

Prompts live in `prompts/<id>_<version>.yaml`, are loaded by (id, version),
and the version is recorded on every run and every step. The active screener
version is PROMPT_VERSION; a request can override it with the
`X-Prompt-Version` header so the evaluation CLI can compare variants on the
same scenarios.
"""
from __future__ import annotations

import functools
import hashlib
from pathlib import Path

import yaml

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


class Prompt:
    def __init__(self, data: dict, path: Path):
        self.id = data["id"]
        self.version = data["version"]
        self.system = data["system"]
        self.user = data["user"]
        self.sha = hashlib.sha256(path.read_bytes()).hexdigest()[:12]

    @property
    def tag(self) -> str:
        return f"{self.id}@{self.version}#{self.sha}"

    def render(self, **kw) -> list[dict]:
        return [{"role": "system", "content": self.system},
                {"role": "user", "content": self.user.format(**kw)}]


@functools.lru_cache(maxsize=32)
def load(prompt_id: str, version: str) -> Prompt:
    path = PROMPT_DIR / f"{prompt_id}_{version}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No prompt {prompt_id} version {version} ({path})")
    return Prompt(yaml.safe_load(path.read_text()), path)


def available(prompt_id: str) -> list[str]:
    return sorted(p.stem.split("_", 1)[1] for p in PROMPT_DIR.glob(f"{prompt_id}_*.yaml"))
