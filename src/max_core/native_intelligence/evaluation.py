"""Deterministic *next-byte* held-out evaluation for MAX's tiny research model.

This does not measure whether generated code compiles, solves tasks, or is safe.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from max_core.native_intelligence.runtime import (
    METADATA_FILE,
    F,
    _corpus,
    load,
    torch,
)

MAX_EVAL_WINDOWS = 128
EVAL_BATCH_SIZE = 16


def evaluate_weights(
    model: Any, data: bytes, *, max_windows: int = MAX_EVAL_WINDOWS
) -> dict[str, float | int]:
    """Compute deterministic teacher-forced next-byte NLL over sampled windows."""
    if not 1 <= max_windows <= MAX_EVAL_WINDOWS:
        raise ValueError("Evaluation window count is out of bounds")
    context = model.config.context
    if len(data) <= context:
        raise ValueError("Held-out data must exceed the context length")

    available = len(data) - context
    windows = min(available, max_windows)
    starts = [(idx * available) // windows for idx in range(windows)]
    tokens = torch.tensor(list(data), dtype=torch.long)
    device = next(model.parameters()).device
    total_loss = 0.0
    total_targets = 0
    was_training = model.training
    model.eval()
    try:
        with torch.inference_mode():
            for index in range(0, windows, EVAL_BATCH_SIZE):
                batch = starts[index : index + EVAL_BATCH_SIZE]
                x = torch.stack([tokens[s : s + context] for s in batch]).to(device)
                y = torch.stack([tokens[s + 1 : s + context + 1] for s in batch]).to(device)
                predictions = model(x)
                total_loss += float(
                    F.cross_entropy(
                        predictions.reshape(-1, 256),
                        y.reshape(-1),
                        reduction="sum",
                    ).cpu()
                )
                total_targets += int(y.numel())
    finally:
        model.train(was_training)

    nll = total_loss / total_targets
    return {
        "evaluated_windows": windows,
        "evaluated_target_bytes": total_targets,
        "next_byte_nll": round(nll, 6),
        "bits_per_byte": round(nll / math.log(2), 6),
        "perplexity": round(math.exp(min(nll, 80.0)), 6),
    }


def evaluate_checkpoint(
    checkpoint: Path, files: list[Path], *, max_windows: int = MAX_EVAL_WINDOWS
) -> dict[str, float | int | str]:
    """Score only explicitly selected held-out files; never run supplied code."""
    model = load(checkpoint)
    data = _corpus(files, model.config.context)
    metadata = json.loads((checkpoint / METADATA_FILE).read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("Invalid checkpoint metadata")
    digest = hashlib.sha256(data).hexdigest()
    if digest == metadata.get("training_sha256"):
        raise ValueError("Refusing exact training-corpus evaluation: data leakage")
    metrics: dict[str, float | int | str] = {
        "model_architecture": "max_byte_causal_transformer_v1",
        "heldout_sha256": digest,
        "heldout_bytes": len(data),
    }
    metrics.update(evaluate_weights(model, data, max_windows=max_windows))
    return metrics
