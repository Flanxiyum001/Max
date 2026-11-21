# MAX Native Intelligence — first-party training prototype

**Status: research-only, not a usable general assistant or coding agent.**

This opt-in experiment creates an original (randomly initialized) byte-level causal Transformer. It trains on **only files explicitly supplied by the owner**, writes local weights and metadata, and generates text using those weights. It never calls a hosted AI API, downloads pretrained weights/tokenizers, scrapes code, reads the whole repository, executes generated code, or grants any MAX Trust permissions. PyTorch is used as a numerical training library, **not** as a third-party trained model. No pretrained model is included.

## What it does not do

The tiny default model is not comparable to Claude, GPT or other frontier models. It does not understand software architecture, provide reliable coding assistance, automatically act on GitHub, converse via MAX Desktop, or improve itself autonomously. Training a frontier-capable model from scratch requires substantial legally sourced training data, specialized computing, research, evaluation, and security work. A successful smoke test is not evidence of practical language intelligence.

## Local usage

```bash
python -m pip install -e '.[native-intelligence]'
python -m max_core.native_intelligence train --input ./my-permitted-training-sample.txt --output ./private/max-model-0001 --steps 100
python -m max_core.native_intelligence sample --checkpoint ./private/max-model-0001 --prompt 'def add(' --tokens 100
```

Use only data you own or have the right to use for model training; never submit credentials, personal documents, licensed proprietary datasets, or production secrets. The original model is initialized from scratch for **each new training run**; the CLI rejects existing output directories. The training metadata contains corpus byte count and SHA-256 fingerprint, but not raw data or source paths. Learned weights can still memorize and reveal training material. Treat the resulting files as private and do not publish them by default.

`--device cuda` opts into GPU training if the locally installed PyTorch has CUDA support. The CPU-only default works for the tiny configuration but cannot train a competitive frontier model. Model files are loaded as weight-only tensors, shape checked and checked against a local digest; the digest is for accidental modification detection, not provenance attestation against a malicious filesystem owner. Avoid untrusted checkpoint folders. Both training and sampling are synchronous and owner-invoked; this experiment adds **no** network listener or side effects beyond writing the owner-selected output directory.

## Training, validation, and measurements

The research CLI also accepts an independent validation file and allows controlled
changes to the tiny architecture. Example (substitute your own licensed, non-sensitive
text files; do not train on proprietary source without the necessary rights):

```bash
python -m max_core.native_intelligence train --input ./training.txt --validation-input ./heldout.txt --output ./private/max-model-0002 --context 64 --width 64 --layers 2 --heads 4 --steps 200
python -m max_core.native_intelligence evaluate --checkpoint ./private/max-model-0002 --input ./new-heldout.txt --max-windows 128
python -m max_core.native_intelligence sample --checkpoint ./private/max-model-0002 --prompt "def " --tokens 100 --seed 17 --temperature 0.8 --top-k 32
```

- `train --validation-input` evaluates a separate file **after** training and records
  loss, bits-per-byte and perplexity. It does not backpropagate on validation data
  or use the score to choose a checkpoint. No automatic early stopping, gradient
  accumulation, parameter-efficient fine-tuning or resume training exists.
- `evaluate` scores only owner-named held-out files on deterministic,
  uniformly spaced teacher-forced windows (maximum 128). Lower next-byte
  negative log likelihood, bits-per-byte and perplexity indicate better
  token prediction **on that dataset only**; these are not measures of usable
  code generation, reasoning, alignment or security.
- An evaluation corpus byte-for-byte identical to the full training corpus
  is rejected. **This does not guarantee uncontaminated evaluation.** Shared
  snippets, near-duplicates and copies of individual training files in larger
  corpora can still leak. Carefully construct independent test sets.
- `--temperature` and `--top-k` change sampling randomness, not model skill.
  The output may be incoherent or unsafe code; it must never be executed
  automatically.
- The configurable settings permit small-scale experiments. Large values
  can exhaust system RAM/VRAM. Run only with adequate resources and monitor
  utilization; results are not comparable to commercial frontier LLMs.

A separate `MAX offline native model research` CI workflow installs CPU-only
PyTorch and runs the real model tests on pull requests. The original general
core CI still verifies unrelated MAX components without installing PyTorch.

## Validation and next gates

- Unit tests exercise original initialization, local training, deterministic sampling, held-out metrics, duplicate-corpus rejection, model configuration, input validation, no-overwrite, and changed-checkpoint denial. Core-only CI skips the opt-in tests; the dedicated CPU model workflow executes them.
- The held-out next-byte evaluation is an elementary language-model metric, **not** a functional coding benchmark. No real assistant workflow or software quality claim is made.
- Next stages: collect permitted curated code data with provenance, build a genuinely independent held-out *functional* coding benchmark (isolated and never executing untrusted code on the host), improve tokenizer, architecture and scalable training, establish signed model provenance and privacy review; only then add an opt-in **read-only** chat adapter to MAX Core. Execution of shell commands, editing repositories, GitHub writes and deployments must remain separately gated by MAX Trust, sandboxing and owner approval.
- Rollback: remove `src/max_core/native_intelligence/`, the optional `native-intelligence` dependency, tests and this document. No existing Gateway, task, or Trust behavior changes.
