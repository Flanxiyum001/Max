"""Optional offline model tests (torch required; no model weights downloaded)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("torch")

from max_core.native_intelligence.runtime import ByteTransformer, Config, load, sample, train


def test_original_model_trains_and_samples_offline(tmp_path: Path) -> None:
    source = tmp_path / "permitted-example.txt"
    source.write_bytes(b"def add(a, b):\n    return a + b\n" * 50)
    checkpoint = tmp_path / "weights"
    result = train(
        [source],
        checkpoint,
        config=Config(context=8, width=16, layers=1, heads=2),
        steps=3,
        batch_size=2,
        seed=9,
    )
    assert result["origin"] == "trained_from_random_initialization"
    assert result["training_bytes"] == source.stat().st_size
    assert isinstance(load(checkpoint), ByteTransformer)
    assert sample(checkpoint, "def ", tokens=4, seed=11) == sample(
        checkpoint, "def ", tokens=4, seed=11
    )


def test_training_refuses_implicit_or_unsafe_inputs(tmp_path: Path) -> None:
    source = tmp_path / "tiny"
    source.write_bytes(b"hi")
    config = Config(context=8, width=16, layers=1, heads=2)
    with pytest.raises(ValueError, match="exceed"):
        train([source], tmp_path / "ckpt", config=config, steps=1)
    with pytest.raises(ValueError, match="explicitly"):
        train([], tmp_path / "ckpt", config=config)
    source.write_bytes(b"hello world" * 10)
    linked = tmp_path / "linked"
    linked.symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        train([linked], tmp_path / "ckpt", config=config)


def test_checkpoint_digest_and_no_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "data"
    source.write_bytes(b"hello world" * 30)
    checkpoint = tmp_path / "weights"
    config = Config(context=8, width=16, layers=1, heads=2)
    train([source], checkpoint, config=config, steps=1, batch_size=1)
    with pytest.raises(FileExistsError):
        train([source], checkpoint, config=config, steps=1)
    file = checkpoint / "model.pt"
    file.write_bytes(file.read_bytes() + b"malformed")
    with pytest.raises(ValueError, match="digest mismatch"):
        load(checkpoint)
