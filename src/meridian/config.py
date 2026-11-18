"""Explicit OpenAI configuration; credentials are never stored in project files."""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OpenAISettings:
    model: str
    api_key: str = field(repr=False)

    @classmethod
    def from_env(cls, model=None):
        chosen = (model if model is not None else os.environ.get('OPENAI_MODEL', '')).strip()
        key = os.environ.get('OPENAI_API_KEY', '').strip()
        if not chosen:
            raise ValueError('Set OPENAI_MODEL or pass --model; no model is selected automatically.')
        if not key:
            raise ValueError('Set OPENAI_API_KEY locally before making a live request.')
        return cls(chosen, key)
