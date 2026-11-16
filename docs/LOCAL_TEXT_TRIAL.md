# Local single-turn text trial (review branch)

This command is an explicit, one-prompt development smoke test of the existing
gateway. It does not start an assistant session, create a task, run a model tool
proposal, control the PC, save chat history or enable a server. Run it only on a
trusted local machine with a **public, non-sensitive test prompt**. It has no
connection to the draft Milestone 9 task planner.

## Setup on Windows PowerShell

Install Python 3.11 or 3.13, then from the repository checkout:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

Set the following variables in the current PowerShell session after reviewing
the actual model, endpoint, inference destination and published token rates.
The endpoint must be the complete Chat Completions or Anthropic Messages URL;
the model identifier must be the exact one accepted by that endpoint. The
example values below are placeholders and **will not connect to FreeLLMAPI**.

```powershell
$env:MAX_TRIAL_ENDPOINT = 'https://reviewed.example/v1/chat/completions'
$env:MAX_TRIAL_MODEL = 'reviewed-model'
$env:MAX_TRIAL_CONTEXT_TOKENS = '8192'
$env:MAX_TRIAL_DIALECT = 'openai_chat'
$env:MAX_TRIAL_DESTINATION = 'cloud'
$env:MAX_TRIAL_CLOUD_ALLOWED = 'yes'
$env:MAX_TRIAL_MAX_COST_USD = '0.01'
$env:MAX_TRIAL_INPUT_USD_PER_MILLION = '1'
$env:MAX_TRIAL_OUTPUT_USD_PER_MILLION = '2'
$secret = Read-Host 'Provider API key' -AsSecureString
$env:MAX_TRIAL_API_KEY = [System.Net.NetworkCredential]::new('', $secret).Password
Remove-Variable secret
.\.venv\Scripts\python.exe -m max_core.gateway_trial
```

The cloud prices and spending ceiling are required for admission, but the
gateway's text-envelope estimate and provider usage report are not a guaranteed
charge cap. Do not substitute guessed prices for an unknown route. For a
genuinely local model, use a reviewed loopback endpoint, set
`MAX_TRIAL_ENDPOINT_LOCAL=yes`, `MAX_TRIAL_DESTINATION=verified_local`, and
`MAX_TRIAL_LOCAL_EGRESS_REVIEWED=yes` only after verifying that the model itself
does not proxy inference to the cloud. A loopback FreeLLMAPI proxy is not proof
of local inference. Omit cloud opt-in and pricing if the local model is free.

The prompt is entered with terminal echo disabled, but it is still sent to the
configured provider, and its reply is printed to the terminal. Do not use
confidential information or assume shell history, terminal scrollback or the
provider retains nothing. The command makes at most one model request; errors
print fixed codes without URL, credential, provider body or prompt. No fallback
or automatic retry occurs. A tool proposal in the model reply is ignored.

## Boundaries and rollback

This is a local developer trial, not a production client or authenticated
Windows companion. It does not verify provider billing, quotas, DNS/egress
controls, provider route pinning or actual compatibility. Tests use an HTTP mock.
To roll back, close the terminal session to discard its environment variables
and remove this branch; no schema migration or persisted app data is created.
