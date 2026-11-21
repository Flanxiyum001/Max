"""Train a tiny *original* byte-level causal Transformer from random initialization.

This is a research/learning prototype, NOT a Claude-quality LLM or a general
coding agent. No pretrained model, model registry, downloads or cloud APIs.
Torch is an optional math framework, not a provider of pretrained weights.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    import torch  # type: ignore[import-not-found]
    from torch import nn  # type: ignore[import-not-found]
    from torch.nn import functional as F  # type: ignore[import-not-found]
except ImportError as exc:
    raise ImportError(
        "MAX Native Intelligence needs the optional PyTorch runtime: "
        "pip install -e '.[native-intelligence]'"
    ) from exc

VOCAB_SIZE = 256  # Every byte is a token; no downloaded tokenizer or vocabulary.
MODEL_FILE = "model.pt"
METADATA_FILE = "metadata.json"
MAX_CORPUS_BYTES = 16 * 1024 * 1024
MAX_GENERATE_TOKENS = 2048


@dataclass(frozen=True)
class Config:
    context: int = 64
    width: int = 64
    layers: int = 2
    heads: int = 4

    def __post_init__(self) -> None:
        if not 8 <= self.context <= 2048:
            raise ValueError("context must be 8..2048")
        if not 16 <= self.width <= 2048 or self.width % self.heads:
            raise ValueError("width must be divisible by heads, within 16..2048")
        if not 1 <= self.layers <= 24 or not 1 <= self.heads <= 32:
            raise ValueError("layers/heads out of bounds")


class Block(nn.Module):  # type: ignore[misc]
    def __init__(self, config: Config) -> None:
        super().__init__()
        self.norm1 = nn.LayerNorm(config.width)
        self.attention = nn.MultiheadAttention(config.width, config.heads, batch_first=True)
        self.norm2 = nn.LayerNorm(config.width)
        self.mlp = nn.Sequential(
            nn.Linear(config.width, 4 * config.width),
            nn.GELU(),
            nn.Linear(4 * config.width, config.width),
        )

    def forward(self, x: Any) -> Any:
        # True in the mask means a future token must be hidden.
        t = x.shape[1]
        mask = torch.ones((t, t), dtype=torch.bool, device=x.device).triu(1)
        normalized = self.norm1(x)
        attended, _ = self.attention(
            normalized, normalized, normalized, attn_mask=mask, need_weights=False
        )
        x = x + attended
        return x + self.mlp(self.norm2(x))


class ByteTransformer(nn.Module):  # type: ignore[misc]
    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.token_embedding = nn.Embedding(VOCAB_SIZE, config.width)
        self.position_embedding = nn.Embedding(config.context, config.width)
        self.blocks = nn.ModuleList([Block(config) for _ in range(config.layers)])
        self.norm = nn.LayerNorm(config.width)
        self.output = nn.Linear(config.width, VOCAB_SIZE, bias=False)

    def forward(self, token_ids: Any) -> Any:
        batch, count = token_ids.shape
        if count < 1 or count > self.config.context:
            raise ValueError("sequence length outside configured context")
        positions = torch.arange(count, device=token_ids.device)
        x = self.token_embedding(token_ids) + self.position_embedding(positions)[None, :, :]
        for block in self.blocks:
            x = block(x)
        return self.output(self.norm(x))


def _corpus(paths: list[Path], context: int) -> bytes:
    if not paths:
        raise ValueError("At least one explicitly named training file is required")
    chunks: list[bytes] = []
    total = 0
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Training input must be a regular, non-symlink file")
        size = path.stat().st_size
        if size > MAX_CORPUS_BYTES or total + size > MAX_CORPUS_BYTES:
            raise ValueError("Research corpus exceeds 16 MiB limit")
        chunks.append(path.read_bytes())
        total += size
    data = b"\n".join(chunks)
    if len(data) <= context:
        raise ValueError("Training corpus must exceed the model context length")
    return data


def train(
    files: list[Path],
    output_dir: Path,
    *,
    config: Config,
    steps: int = 100,
    batch_size: int = 8,
    learning_rate: float = 0.001,
    seed: int = 7,
    device: str = "cpu",
) -> dict[str, Any]:
    """Create new model weights. Never loads existing weights or contacts a service."""
    if not 1 <= steps <= 1_000_000 or not 1 <= batch_size <= 256:
        raise ValueError("Invalid steps/batch size")
    if not 0 < learning_rate <= 0.01 or not 0 <= seed < 2**32:
        raise ValueError("Invalid learning rate or seed")
    if device not in {"cpu", "cuda"} or (device == "cuda" and not torch.cuda.is_available()):
        raise ValueError("Requested compute device is unavailable")
    data = _corpus(files, config.context)
    if output_dir.exists():
        raise FileExistsError("Output directory already exists: refusing to overwrite")

    torch.manual_seed(seed)
    model = ByteTransformer(config).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    tokens = torch.tensor(list(data), dtype=torch.long)
    maximum_start = len(tokens) - config.context
    model.train()
    initial_loss: float | None = None
    final_loss = 0.0
    for _ in range(steps):
        starts = torch.randint(0, maximum_start, (batch_size,))
        x = torch.stack([tokens[int(s) : int(s) + config.context] for s in starts]).to(device)
        y = torch.stack(
            [tokens[int(s) + 1 : int(s) + config.context + 1] for s in starts]
        ).to(device)
        optimizer.zero_grad(set_to_none=True)
        predictions = model(x)
        loss = F.cross_entropy(predictions.reshape(-1, VOCAB_SIZE), y.reshape(-1))
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        final_loss = float(loss.detach().cpu())
        if initial_loss is None:
            initial_loss = final_loss

    output_dir.mkdir(parents=True, exist_ok=False)
    # No raw training content or source paths in the metadata file.
    metadata: dict[str, Any] = {
        "format": 1,
        "origin": "trained_from_random_initialization",
        "architecture": "max_byte_causal_transformer_v1",
        "config": asdict(config),
        "steps": steps,
        "seed": seed,
        "training_bytes": len(data),
        "training_sha256": hashlib.sha256(data).hexdigest(),
        "first_batch_loss": initial_loss,
        "last_batch_loss": final_loss,
        "quality_claim": "research_only_not_frontier_capable",
    }
    model_path = output_dir / MODEL_FILE
    torch.save(model.cpu().state_dict(), model_path)
    metadata["weights_sha256"] = hashlib.sha256(model_path.read_bytes()).hexdigest()
    (output_dir / METADATA_FILE).write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def load(checkpoint: Path) -> ByteTransformer:
    """Load strictly shaped tensors only; never unpickle arbitrary Python objects."""
    metadata = json.loads((checkpoint / METADATA_FILE).read_text(encoding="utf-8"))
    if metadata.get("format") != 1 or metadata.get("origin") != "trained_from_random_initialization":
        raise ValueError("Unsupported or unverified model metadata")
    if metadata.get("architecture") != "max_byte_causal_transformer_v1":
        raise ValueError("Unsupported model architecture")
    allowed = {"context", "width", "layers", "heads"}
    values = metadata.get("config")
    if not isinstance(values, dict) or values.keys() != allowed:
        raise ValueError("Invalid model configuration")
    if any(type(value) is not int for value in values.values()):
        raise ValueError("Invalid configuration types")
    config = Config(**values)
    weights_path = checkpoint / MODEL_FILE
    if weights_path.is_symlink() or not weights_path.is_file():
        raise ValueError("Checkpoint must be a regular, non-symlink file")
    if hashlib.sha256(weights_path.read_bytes()).hexdigest() != metadata.get("weights_sha256"):
        raise ValueError("Checkpoint digest mismatch")
    model = ByteTransformer(config)
    state = torch.load(weights_path, map_location="cpu", weights_only=True)
    model.load_state_dict(state, strict=True)
    model.eval()
    return model


@torch.inference_mode()
def sample(checkpoint: Path, prompt: str, *, tokens: int = 120, seed: int = 17) -> str:
    if not 1 <= tokens <= MAX_GENERATE_TOKENS or not 0 <= seed < 2**32:
        raise ValueError("Invalid sampling bounds")
    if len(prompt.encode("utf-8")) > 8_192:
        raise ValueError("Prompt is too long")
    model = load(checkpoint)
    input_bytes = list(prompt.encode("utf-8")) or [10]
    rng = torch.Generator(device="cpu").manual_seed(seed)
    for _ in range(tokens):
        context = torch.tensor([input_bytes[-model.config.context :]], dtype=torch.long)
        logits = model(context)[0, -1, :]
        probability = torch.softmax(logits, dim=-1)
        next_byte = int(torch.multinomial(probability, 1, generator=rng).item())
        input_bytes.append(next_byte)
    return bytes(input_bytes).decode("utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAX native model research (offline, from scratch)")
    commands = parser.add_subparsers(dest="command", required=True)
    trainer = commands.add_parser("train", help="Train only on explicitly listed, permitted files")
    trainer.add_argument("--input", action="append", type=Path, required=True)
    trainer.add_argument("--output", type=Path, required=True)
    trainer.add_argument("--steps", type=int, default=100)
    trainer.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    generator = commands.add_parser("sample", help="Generate sample bytes from a trained model")
    generator.add_argument("--checkpoint", type=Path, required=True)
    generator.add_argument("--prompt", required=True)
    generator.add_argument("--tokens", type=int, default=120)
    args = parser.parse_args(argv)
    if args.command == "train":
        result = train(
            args.input, args.output, config=Config(), steps=args.steps, device=args.device
        )
        print(json.dumps(result, indent=2))
    else:
        print(sample(args.checkpoint, args.prompt, tokens=args.tokens))
    return 0
