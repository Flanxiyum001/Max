# Promptfoo evaluation layer (optional)

This directory holds MAX's **versioned adversarial evaluation dataset**, kept
provider-agnostic so it can be consumed either by MAX's own deterministic
suite (default, offline, no credentials) or by [Promptfoo](https://github.com/promptfoo/promptfoo)
as a supplementary layer.

## Why Promptfoo is not wired in by default

Promptfoo is a **Node ≥22** tool with a large JS dependency graph. MAX is a
Python project whose install path is `pip install -e '.[dev]'`. Running two
runtimes in CI for evaluations that MAX already executes deterministically is
not justified without a measured need — see `docs/INTEGRATIONS.md` §4.

## Run with Promptfoo (when you want it)

```bash
npm install -g promptfoo@latest          # requires Node >= 22
promptfoo eval -c eval/promptfoo/promptfoo.yaml
```

*Offline cases* run against the mock provider and cost nothing.
*`provider_backed: true`* cases (e.g. `adv-13-provider-regression`) call a real
model: they require credentials and may incur charges. They are never part of
the default gate.

## Run offline (what CI does)

```bash
.venv/bin/python -m pytest tests/test_adversarial_dataset.py -q
.venv/bin/python -m pytest -q            # full suite
.venv/bin/python -m max eval run         # existing 14-scenario suite
```

## Dataset contract

Each case has: `id`, `category`, `input`, `expected`, `assertion`,
`provider_backed`. Expected outcomes are drawn from
`assertions_legend` — `tool_rejected`, `action_blocked`, `no_data_leak`,
`flagged`, `bounded`, `requires_human`.

No case is marked as "detected" unless a test actually produced that result.
