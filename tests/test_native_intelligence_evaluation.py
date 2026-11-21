"""Explicitly opted-in, offline evaluation tests (PyTorch required)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("torch")

from max_core.native_intelligence.evaluation import (  # noqa: E402
    evaluate_checkpoint,
    evaluate_weights,
)
from max_core.native_intelligence.runtime import (  # noqa: E402
    Config,
    load,
    main,
    sample,
    train,
)


def _fixtures(tmp_path: Path) -> tuple[Path, Path, Path, Config]:
    training = tmp_path / "training.txt"
    validation = tmp_path / "heldout.txt"
    training.write_bytes(b"def double(x):\n    return x * 2\n" * 40)
    validation.write_bytes(b"def triple(y):\n    return y * 3\n" * 40)
    return training, validation, tmp_path / "model", Config(context=8, width=16, layers=1, heads=2)


def test_train_records_heldout_metrics_without_retraining_on_validation(
    tmp_path: Path,
) -> None:
    training, validation, checkpoint, config = _fixtures(tmp_path)
    result = train(
        [training],
        checkpoint,
        config=config,
        validation_files=[validation],
        steps=2,
        batch_size=2,
        seed=19,
    )
    metrics = result["validation"]
    assert metrics["heldout_sha256"] == hashlib.sha256(validation.read_bytes()).hexdigest()
    assert metrics["heldout_bytes"] == len(validation.read_bytes())
    assert metrics["evaluated_windows"] > 0
    assert metrics["next_byte_nll"] > 0
    assert result["quality_claim"] == "research_only_not_frontier_capable"


def test_evaluation_is_deterministic_and_rejects_exact_training_copy(tmp_path: Path) -> None:
    training, validation, checkpoint, config = _fixtures(tmp_path)
    train([training], checkpoint, config=config, steps=1, batch_size=2)
    first = evaluate_checkpoint(checkpoint, [validation], max_windows=4)
    assert first == evaluate_checkpoint(checkpoint, [validation], max_windows=4)
    assert first["evaluated_windows"] == 4
    assert first["evaluated_target_bytes"] == config.context * 4
    assert first["model_architecture"] == "max_byte_causal_transformer_v1"
    assert first["next_byte_nll"] > 0
    with pytest.raises(ValueError, match="data leakage"):
        evaluate_checkpoint(checkpoint, [training])
    with pytest.raises(ValueError, match="window count"):
        evaluate_checkpoint(checkpoint, [validation], max_windows=129)


def test_rejects_validation_identical_to_training(tmp_path: Path) -> None:
    training, _, checkpoint, config = _fixtures(tmp_path)
    with pytest.raises(ValueError, match="differ"):
        train([training], checkpoint, config=config, validation_files=[training], steps=1)
    assert not checkpoint.exists()


def test_sample_top_k_and_temperature_bounds(tmp_path: Path) -> None:
    training, _, checkpoint, config = _fixtures(tmp_path)
    train([training], checkpoint, config=config, steps=1, batch_size=2)
    assert sample(checkpoint, "def ", tokens=5, top_k=1, seed=1) == sample(
        checkpoint, "def ", tokens=5, top_k=1, seed=999
    )
    with pytest.raises(ValueError, match="top-k"):
        sample(checkpoint, "hello", top_k=257)
    with pytest.raises(ValueError, match="Temperature"):
        sample(checkpoint, "hello", temperature=0.0)
    with pytest.raises(ValueError, match="Temperature"):
        sample(checkpoint, "hello", temperature=float("nan"))


def test_cli_evaluate_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    training, validation, checkpoint, config = _fixtures(tmp_path)
    train([training], checkpoint, config=config, steps=1, batch_size=1)
    status = main(
        [
            "evaluate",
            "--checkpoint",
            str(checkpoint),
            "--input",
            str(validation),
            "--max-windows",
            "2",
        ]
    )
    assert status == 0
    assert '"evaluated_windows": 2' in capsys.readouterr().out


def test_evaluation_weights_preserves_model_mode(tmp_path: Path) -> None:
    training, validation, checkpoint, config = _fixtures(tmp_path)
    train([training], checkpoint, config=config, steps=1, batch_size=1)
    model = load(checkpoint)
    model.train()
    assert model.training is True
    result = evaluate_weights(model, validation.read_bytes(), max_windows=2)
    assert result["evaluated_windows"] == 2
    assert model.training is True
