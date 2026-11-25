# SOP Ticket Workbench User and Integration Manual

This manual explains how to run the SOP ingestion and ticket workflows in
manual/external-LLM mode, and how can we add an approved
LLM provider integration without replacing the existing workflows.

## 1. Current behavior

The project has two related but separate workflows:

1. **SOP ingestion** prepares source documents and prompts. The default provider
   adapter is intentionally unconfigured, so jobs stop at `LLM_REQUIRED`.
   After an LLM response is supplied, `--resume` validates it and creates the
   catalog and processed SOP content.
2. **Ticket processing** builds an SOP-matching prompt and then a resolution
   proposal prompt. The API and web UI do not call an LLM; a person runs each
   prompt in Copilot or another approved LLM and pastes the first response into
   the next step.

Neither workflow updates a ServiceNow/Pulse ticket or performs production
actions. The ticket proposal is for a support engineer to review.

## 2. Important files and integration points

| File | Purpose |
| --- | --- |
| `offline_llm_sop_pipeline.py` | Prepares SOP jobs, validates model output, and creates ingestion outputs. |
| `ticket_sop_agent.py` | FastAPI app, prompt builder, ingestion-start/status API, and ticket prompt endpoints. |
| `ticket_sop_ui.html` | Local browser interface for starting ingestion and completing the two manual ticket-prompt steps. |
| `PROMPT_RETRIEVAL.txt` | SOP matching instructions and JSON output contract. |
| `PROMPT_PROPOSAL.txt` | Ticket resolution proposal instructions and JSON output contract. |
| `SOP_Metadata_All.xlsx` | Source metadata and approved tag mappings, when available. |
| `approved_tags.json` | Approved-tag fallback when the workbook does not supply tags. |
| `requirements_offline_llm.txt` | Ingestion dependencies. |
| `requirements_ticket_agent.txt` | API and prompt-building dependencies. |

### Ingestion provider hook

Implement a provider in `offline_llm_sop_pipeline.py` at:

```python
invoke_llm(prepared_file: Path, prompt: str, schema: dict) -> Optional[dict]
```

The function receives the prepared document, its extraction prompt, and the
Pydantic-derived JSON Schema. It currently returns `None`, which keeps the
pipeline offline. A successful provider implementation must return the parsed
JSON object; `call_llm_placeholder()` then saves it to the job's
`expected_response_file`. Do not write the catalog or move source files in the
provider adapter. `--resume` owns validation and materialization.

### Ticket provider hook

Ticket provider calls are **not implemented**. `ticket_sop_agent.py` builds
prompts through `MATCH_PROMPT_TEMPLATE`, `PROPOSAL_PROMPT_TEMPLATE`, and the
LangGraph prompt-building nodes. The existing routes return prompts only:

- `POST /agent/match-prompt`
- `POST /agent/proposal-prompts`

To add an integrated ticket LLM, add a provider adapter and call it after the
existing prompt is built. Keep prompt construction, model invocation, response
validation, and UI display as separate responsibilities. Prefer adding a new
integrated route, such as `POST /agent/process-ticket`, while preserving the
manual routes and the current provider-off-by-default behavior.

## 3. Setup and start the local workbench

Run commands from the project directory:

```powershell
Set-Location "C:\Users\<user>\Downloads\Ingestion Cycle"
py -3.12 -m pip install -r .\requirements_offline_llm.txt -r .\requirements_ticket_agent.txt
$env:SOP_INGESTION_ROOT = (Get-Location).Path
.\.venv\Scripts\python.exe -m uvicorn ticket_sop_agent:app --host 127.0.0.1 --port 8000
```

If the virtual environment has a different name, use that environment's
`python.exe`. Open `http://127.0.0.1:8000/` in a browser. The service binds to
localhost in this command and should remain local unless deployment security is
designed separately.

By default, the API reads these files relative to the project directory:

- `SOP_JSON.json`
- `sop_content\`
- `PROMPT_RETRIEVAL.txt`
- `PROMPT_PROPOSAL.txt`

Optional environment overrides are `SOP_INGESTION_ROOT`, `SOP_CATALOG_PATH`,
`SOP_CONTENT_DIR`, `SOP_MATCH_PROMPT_FILE`, and `SOP_PROPOSAL_PROMPT_FILE`.
When using overrides, ensure the paths identify the intended project data and
prompt files.

## 4. External LLM workflow (manual Copilot or another approved LLM)

This mode requires no provider code changes. Use only an LLM approved for the
data being processed.

### 4.1 Prepare and ingest SOP documents

1. Place source SOP documents under `eDelivery SOP\`. Supported formats are
   defined by `SUPPORTED` in `offline_llm_sop_pipeline.py`.
2. Ensure an approved-tag source is available: the metadata workbook or
   `approved_tags.json`.
3. In the web UI, select **Start ingestion cycle**, or run:

   ```powershell
   .\.venv\Scripts\python.exe .\offline_llm_sop_pipeline.py
   ```

4. The pipeline creates one job directory per source under `llm_pending\`.
   Each job contains:
   - `prepared\`: the document prepared for the LLM (Office files are rendered
     to PDF when LibreOffice is available).
   - `prompt.txt`: extraction instructions and approved tags.
   - `response_schema.json`: required JSON Schema.
   - `request.json`: job status, file paths, and expected response path.
5. For each job, submit the prepared document, `prompt.txt`, and
   `response_schema.json` to the approved LLM. Require one JSON object that
   conforms to the schema. Save the raw JSON object to that job's
   `llm_response.json` path (also recorded in `request.json`). Do not save
   Markdown fences or explanatory text around the JSON.
6. After responses are saved, validate and materialize them:

   ```powershell
   .\.venv\Scripts\python.exe .\offline_llm_sop_pipeline.py --resume
   ```

7. Check the command results and each job's `request.json`. Successful jobs
   become `COMPLETE`; unresolved jobs remain `LLM_REQUIRED`. Successful
   processing updates `SOP_JSON.json`, `sop_content\`, and
   `sop_filter_content\`, and moves the original source to `processed_SOP\`.

The **Start ingestion cycle** button performs preparation only; it does not
submit files to an LLM or run `--resume`.

### 4.2 Match a ticket to SOPs

1. In **Process ticket**, paste either one ticket object or a
   `{"tickets": [...]}` object. Use the original ticket JSON, including the
   ticket number/ID.
2. Select **Generate SOP matching prompt**. The UI sends the ticket to
   `POST /agent/match-prompt`.
3. Copy the generated prompt and submit it to the approved LLM.
4. Require the exact JSON contract in `PROMPT_RETRIEVAL.txt`: one top-level
   `results` array, with one result per input ticket, matching ticket IDs, and
   `full_match` and `partial_match` arrays.
5. Paste the raw JSON response into **Copilot matching response JSON**. It must
   be valid JSON (no code fences, comments, or unescaped quotation marks).

### 4.3 Generate the resolution proposal prompt

1. Keep the same original ticket JSON in the ticket field.
2. Select **Continue to resolution proposal**. The UI sends the original
   ticket and match response to `POST /agent/proposal-prompts`.
3. The service verifies that each ticket has exactly one matching result,
   resolves selected SOP IDs against `SOP_JSON.json`, and loads each selected
   SOP's complete content from `sop_content\`.
4. Copy the generated proposal prompt and submit it to the approved LLM.
5. Review the resulting JSON according to `PROMPT_PROPOSAL.txt`. The expected
   output contains `ticket_id`, `ticket_open_at`, and `proposal` with
   `tags`, `plan`, and `next_step`. It is a recommendation, not an execution
   record.

If a selected SOP has no processed content, the proposal request cannot be
completed. A catalog entry alone (including a dummy catalog) is not a substitute
for the corresponding `sop_content\*_SOP_CONTENT.json` file.

## 5. Integrated LLM workflow: implementation guide for integrating a LLM

Use this section when changing the project to call an LLM programmatically.
First select an organization-approved provider and confirm that the documents
and ticket data may be sent to it.

### 5.1 Shared provider requirements

1. Add provider/model configuration through environment variables or the
   existing approved secret store. Never hard-code API keys, tokens, or
   credentials, and never write them to logs or source control.
2. Keep the provider SDK isolated behind a small adapter. Do not place
   provider-specific code in prompt templates or ingestion materialization.
3. Set explicit request timeouts and bounded retries for transient errors.
   Surface authentication, rate-limit, timeout, and invalid-response errors;
   do not convert failures into success-shaped empty JSON.
4. Return parsed JSON objects, not raw Markdown. Validate all model output
   locally before writing it or returning it to the UI.
5. Preserve the manual/external mode and make integrated mode explicit.
   Do not send customer data to an external provider without approval.

### 5.2 Integrate SOP extraction

1. Implement `invoke_llm(prepared_file, prompt, schema)` in
   `offline_llm_sop_pipeline.py` or delegate it to a new provider-adapter
   module.
2. Submit the actual prepared file as a document/image input supported by the
   selected model; do not replace visual-document input with text-only
   extraction when screenshots, tables, or diagrams matter.
3. Pass the supplied prompt and enforce `schema` using the provider's
   structured-output/JSON-schema feature where supported.
4. Parse the provider response into a dictionary and return it. Keep the
   existing `call_llm_placeholder()` behavior: it writes the response to the
   job's `expected_response_file`.
5. Run `offline_llm_sop_pipeline.py` to prepare and process sources, then run
   `offline_llm_sop_pipeline.py --resume` to validate and materialize the
   saved responses. Verify catalog IDs, output files, tags, and job status.
6. Add tests using a mocked provider response: valid schema, invalid schema,
   provider timeout/error, and a response that includes unapproved tags.
   Tests must not call a live provider.

### 5.3 Integrate ticket matching and proposal generation

1. Add a ticket-provider adapter that accepts a completed prompt and returns a
   parsed JSON object. Use structured output or equivalent validation. The
   retrieval contract is in `PROMPT_RETRIEVAL.txt`; the proposal contract is
   in `PROMPT_PROPOSAL.txt`.
2. For matching, reuse the existing prompt builder and catalog. Validate that
   `results` has exactly one entry per input ticket, IDs are preserved, and
   both match groups are arrays before continuing.
3. For proposal generation, reuse the existing SOP selection/content lookup
   and proposal prompt builder. Invoke the model once per returned proposal
   prompt and validate the required fields and ticket ID before returning it.
4. Add an integrated route that runs the sequence:
   `ticket input -> matching prompt -> matching response -> SOP content and
   proposal prompt -> proposal response`. Return both the match result and
   proposal result so the UI can show evidence and recommendation separately.
5. Keep `POST /agent/match-prompt` and `POST /agent/proposal-prompts` for the
   manual workflow. Update `ticket_sop_ui.html` to expose integrated processing
   only when explicitly configured; retain copy/paste steps as a fallback.
6. Do not add any ServiceNow/Pulse write operation. The UI must label generated
   content as a proposal and require human review.
7. Add tests for multi-ticket matching, ticket-ID mismatch, unknown SOP ID,
   missing SOP content, malformed model JSON, provider failures, and successful
   matching-to-proposal flow. Mock all provider calls.

### 5.4 Integration acceptance checklist

- Provider configuration is optional and disabled by default.
- No credentials or ticket/SOP content are logged.
- Manual endpoints and external-LLM workflow continue to work.
- Provider output is validated before it reaches pipeline output or UI.
- A failed provider call is reported explicitly and can be retried safely.
- Ingestion still uses `--resume` for deterministic validation/materialization.
- Ticket output remains a recommendation and never updates a ticket or
  performs a production action.

## 6. API reference

### Build an SOP matching prompt

```http
POST /agent/match-prompt
Content-Type: application/json
```

```json
{
  "tickets": [
    {
      "number": "INC123456",
      "short_description": "Example issue"
    }
  ]
}
```

Returns `{"prompt": "..."}`. `tickets` must be a non-empty array.

### Build proposal prompts

```http
POST /agent/proposal-prompts
Content-Type: application/json
```

```json
{
  "tickets": [
    {
      "number": "INC123456",
      "short_description": "Example issue"
    }
  ],
  "match_response": {
    "results": [
      {
        "ticket_id": "INC123456",
        "full_match": [],
        "partial_match": [],
        "no_match_reason": "No SOP met the minimum evidence requirements."
      }
    ]
  }
}
```

Returns `{"prompts": [{"ticket_id": "...", "prompt": "..."}]}`. The result
ticket ID must match the original ticket ID/number exactly.

## 7. Troubleshooting

| Status/symptom | Meaning and action |
| --- | --- |
| `422 Unprocessable Entity` | Request JSON/schema is invalid. Use a non-empty `tickets` array and, for proposals, a `match_response.results` array with the matching ticket ID. Fix unescaped quotes and remove code fences. |
| `503` missing/invalid file | Check the configured catalog or prompt path and confirm it is valid JSON/text. Prompt files must be `PROMPT_RETRIEVAL.txt` and `PROMPT_PROPOSAL.txt` by default. |
| `409` missing SOP content | The selected SOP ID is not backed by a matching processed content file. Complete ingestion and `--resume`, or correct the catalog/content configuration. |
| `LLM_REQUIRED` | No provider response was supplied. Use the manual external-LLM steps or implement an approved provider adapter. |
| `PREPARATION_FAILED` | Check the job output. Office-to-PDF conversion requires LibreOffice; also verify the source file and permissions. |
| Ingestion says complete but a SOP is absent | Inspect that job's `request.json` and `llm_response.json`; verify `--resume` output and whether model JSON passed Pydantic validation. |

For JSON errors, remember that quotes inside JSON strings must be escaped, for
example: `"message": "The value was \"not found\"."`.
