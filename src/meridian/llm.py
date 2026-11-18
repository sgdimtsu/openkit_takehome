"""One synchronous structured-output request with explicit failure and metrics."""
from __future__ import annotations

import json
from dataclasses import dataclass
from time import perf_counter

from openai import OpenAI


@dataclass
class CallMetrics:
    call_count: int
    model: str
    duration_seconds: float = 0.0
    response_id: str | None = None
    usage: dict | None = None


class LLMCallError(RuntimeError):
    def __init__(self, message, metrics):
        super().__init__(message)
        self.metrics = metrics


def create_client(settings):
    # A retry would be another call. Fail visibly rather than silently repeat it.
    return OpenAI(api_key=settings.api_key, max_retries=0, timeout=60.0)


def call_structured(client, *, model, prompt, payload, output_type):
    metrics = CallMetrics(call_count=1, model=model)
    started = perf_counter()
    try:
        response = client.responses.parse(
            model=model,
            input=[{'role': 'system', 'content': prompt},
                   {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
            text_format=output_type,
            store=False,
        )
        metrics.response_id = response.id
        metrics.model = response.model
        metrics.usage = response.usage.model_dump() if response.usage is not None else None
        if response.status != 'completed':
            raise LLMCallError(f'OpenAI response was {response.status}; no result accepted.', metrics)
        refusals = [item.refusal for output in response.output if output.type == 'message'
                    for item in output.content if item.type == 'refusal']
        if refusals:
            raise LLMCallError('OpenAI refused the request; no result accepted.', metrics)
        if response.output_parsed is None:
            raise LLMCallError('OpenAI returned no structured result.', metrics)
        return output_type.model_validate(response.output_parsed), metrics
    except LLMCallError:
        raise
    except Exception as exc:
        # Do not log credentials, request bodies or arbitrary provider error bodies.
        raise LLMCallError(f'OpenAI request or schema validation failed ({type(exc).__name__}); '
                           'no result accepted.', metrics) from exc
    finally:
        metrics.duration_seconds = perf_counter() - started
