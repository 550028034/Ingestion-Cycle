# Offline LLM-boundary SOP workflow

This project intentionally performs no API call. It preserves document preparation, prompts, strict output schema, job state, validation, catalog upsert, output generation and source-file movement.

See [USER_MANUAL.md](./USER_MANUAL.md) for the full setup, external-LLM operating steps, and implementation guidance for adding approved LLM integrations.

`invoke_llm()` in `offline_llm_sop_pipeline.py` is the provider adapter function called for each prepared job. Implement the provider SDK call there and return the model's parsed JSON object. `call_llm_placeholder()` loads the prepared document, prompt, and response schema, dispatches them to `invoke_llm()`, and saves a returned response to `expected_response_file`. Until a provider is configured, `invoke_llm()` returns `None` and the workflow remains offline with status `LLM_REQUIRED`.

## Phase 1: prepare jobs
```powershell
python .\offline_llm_sop_pipeline.py
```
Each source gets a folder under `llm_pending` containing:
- `prepared/`: the PDF/image/text to submit to an LLM
- `prompt.txt`: complete SOP extraction prompt
- `response_schema.json`: strict expected JSON schema
- `request.json`: status `LLM_REQUIRED`, paths and instructions

The source stays in `eDelivery SOP`.

## Phase 2: supply an LLM result later
Use any approved multimodal LLM manually or from a future provider adapter. Save its schema-conformant JSON as:
```text
llm_pending/<job-id>/llm_response.json
```

## Phase 3: resume deterministically
```powershell
python .\offline_llm_sop_pipeline.py --resume
```
The pipeline validates the response with Pydantic, updates `SOP_JSON.json`, creates the detailed and filtered JSON files, and moves the original source to `processed_SOP`.

## Status meaning
- `LLM_REQUIRED`: preparation succeeded, but no provider call is configured.
- `PREPARATION_FAILED`: Office rendering or filesystem preparation failed.
- `COMPLETE`: a valid LLM response was imported and outputs were created.

## Independent ticket-to-SOP prompt agent

The separate `ticket_sop_agent.py` service uses FastAPI, LangChain prompt templates, and a LangGraph workflow. It does not import or modify the ingestion pipeline and does not make LLM/API calls. It returns ready-to-paste prompts so each LLM step can be tested manually in Copilot.

Use Python 3.12 for the agent dependencies, then start it locally:

```powershell
py -3.12 -m pip install -r .\requirements_ticket_agent.txt
$env:SOP_INGESTION_ROOT = (Get-Location).Path
py -3.12 -m uvicorn ticket_sop_agent:app --host 127.0.0.1 --port 8000
```

Keep `PROMPT_RETRIEVAL.txt` and `PROMPT_PROPOSAL.txt` beside `ticket_sop_agent.py`; they are loaded from the project directory by default. The agent reads `SOP_JSON.json` and `sop_content\` from `SOP_INGESTION_ROOT`. The matching endpoint requires the catalog; the proposal endpoint also requires processed SOP content.

Open `http://127.0.0.1:8000/` for the local web interface. **Start ingestion cycle** runs the preparation pipeline and displays its job summary; it does not call an LLM. Complete any `LLM_REQUIRED` jobs in Copilot and run the documented `--resume` command to materialize their SOP content. **Process ticket** builds a matching prompt from ticket JSON, accepts the matching JSON returned by Copilot, and then builds the resolution proposal prompt for the next Copilot step.

1. `POST http://127.0.0.1:8000/agent/match-prompt` with `{"tickets": [...]}`. Copy the returned `prompt` to Copilot and run it.
2. `POST http://127.0.0.1:8000/agent/proposal-prompts` with the original `tickets` and the Copilot JSON result as `match_response`. The agent resolves the matched SOP IDs against the ingestion catalog, loads their complete processed SOP JSON, and returns one ready-to-paste proposal prompt per ticket.

Example second-stage request:

```json
{
  "tickets": [{"ticket_id": "INC123456", "short_description": "Example issue"}],
  "match_response": {
    "results": [{
      "ticket_id": "INC123456",
      "full_match": [],
      "partial_match": [],
      "no_match_reason": "No SOP met the minimum evidence and score requirements."
    }]
  }
}
```

Set `SOP_CATALOG_PATH` and `SOP_CONTENT_DIR` to override the default ingestion output paths. The service binds to localhost in the example command and keeps the two prompt steps separate because the second requires the first Copilot response.
