# Magnitude in MAX Desktop (Windows)

Use [Magnitude Desktop or its CLI](https://github.com/magnitudedev/magnitude)
to install and select a **local** model. Start Magnitude Desktop, or run
`magnitude serve` if you use the CLI. Keep network access disabled; MAX only
uses `127.0.0.1:10100`. No API key is needed.

From the MAX checkout:

    .\start-max.cmd --check-magnitude
    .\start-max.cmd --magnitude

If multiple models are listed, set `MAX_MAGNITUDE_MODEL` to an exact ID shown
by the check in your `.env`, or set it for the current PowerShell session:

    $env:MAX_MAGNITUDE_MODEL = '<exact ID>'
    .\start-max.cmd --magnitude

The check reads health and model IDs only. It neither loads a checkpoint nor
downloads one. The first reply may be slower while the model loads. Text-only
turns use Magnitude's OpenAI-compatible stream, and the orb shows the completed
answer. No prompt is retried on another backend. Regular `start-max.cmd`, the
Ollama profile and the independent Qwen3-TTS output settings are unchanged.

## Optional read-only workspace assistance

Set `MAX_WORKSPACE_DIR` to a specific repository, and set
`MAX_MAGNITUDE_TOOLS=yes` only for a model that reliably follows tool
instructions. MAX uses its existing portable tool protocol for listing and
reading files in that workspace. It does not enable shell, writes, network,
GitHub, or MCP tools in this mode. If the model does not reliably request the
tools, turn the setting off and paste a relevant excerpt instead.

`MAX_MAGNITUDE_CONTEXT_TOKENS` defaults to 8192. Raising it increases memory
pressure. A faster coding reply doesn't necessarily mean faster voice:
transcription, inference, audio synthesis and playback all contribute to the
delay. Test on your own hardware before choosing a default.

Upstream references: [API overview](https://docs.magnitude.dev/api/overview)
and [endpoint reference](https://docs.magnitude.dev/api/endpoints).
