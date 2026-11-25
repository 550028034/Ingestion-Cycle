"""Local API that builds sequential Copilot prompts from ingested SOP data."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from langchain_core.prompts import PromptTemplate
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field


@dataclass(frozen=True)
class Settings:
    catalog_path: Path
    content_dir: Path
    match_prompt_path: Path
    proposal_prompt_path: Path

    @classmethod
    def from_environment(cls) -> Settings:
        ingestion_root_value = os.environ.get("SOP_INGESTION_ROOT")
        ingestion_root = (
            Path(ingestion_root_value).expanduser().resolve()
            if ingestion_root_value
            else Path(__file__).resolve().parent
        )

        def configured_path(name: str, default: Path) -> Path:
            value = os.environ.get(name)
            path = Path(value).expanduser() if value else default
            return (ingestion_root / path).resolve() if not path.is_absolute() else path.resolve()

        return cls(
            catalog_path=configured_path("SOP_CATALOG_PATH", Path("SOP_JSON.json")),
            content_dir=configured_path("SOP_CONTENT_DIR", Path("sop_content")),
            match_prompt_path=configured_path(
                "SOP_MATCH_PROMPT_FILE", Path("PROMPT_RETRIEVAL.txt")
            ),
            proposal_prompt_path=configured_path(
                "SOP_PROPOSAL_PROMPT_FILE", Path("PROMPT_PROPOSAL.txt")
            ),
        )


class AgentWorkflowError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class AgentState(TypedDict, total=False):
    phase: Literal["match", "proposal"]
    tickets: list[dict[str, Any]]
    match_response: dict[str, Any]
    prompt: str
    prompts: list[dict[str, str]]


class MatchPromptRequest(BaseModel):
    tickets: list[dict[str, Any]] = Field(min_length=1)


class ProposalPromptRequest(BaseModel):
    tickets: list[dict[str, Any]] = Field(min_length=1)
    match_response: dict[str, Any]


MATCH_PROMPT_TEMPLATE = PromptTemplate.from_template(
    "{system_prompt}\n\n"
    "DATA TO PROCESS:\n"
    "TICKETS:\n{tickets_json}\n\n"
    "SOP_CATALOG:\n{sop_catalog_json}"
)

PROPOSAL_PROMPT_TEMPLATE = PromptTemplate.from_template(
    "{system_prompt}\n\n"
    "INPUT DATA\n\n"
    "ORIGINAL_TICKET_JSON:\n{ticket_json}\n\n"
    "SOP_MATCH_RESPONSE_JSON:\n{match_response_json}\n\n"
    "SOP_CONTENT_JSON:\n{sop_content_json}"
)

_INGESTION_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sop-ingestion")
_INGESTION_LOCK = threading.Lock()
_INGESTION_FUTURE: Future[subprocess.CompletedProcess[str]] | None = None


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise AgentWorkflowError(
            503, f"{label} not found at {path}. Run ingestion or check agent configuration."
        ) from exc
    except json.JSONDecodeError as exc:
        raise AgentWorkflowError(503, f"{label} is not valid JSON: {path}") from exc


def _read_prompt_preamble(path: Path, boundary: str, label: str) -> str:
    try:
        prompt = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise AgentWorkflowError(503, f"{label} not found at {path}.") from exc

    boundary_index = prompt.rfind(boundary)
    if boundary_index < 0:
        raise AgentWorkflowError(
            503, f"{label} must contain the expected input boundary {boundary!r}."
        )
    return prompt[:boundary_index].rstrip()


def _load_catalog(settings: Settings) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    catalog = _read_json(settings.catalog_path, "SOP catalog")
    if not isinstance(catalog, list) or any(not isinstance(item, dict) for item in catalog):
        raise AgentWorkflowError(503, "SOP catalog must be a JSON array of SOP objects.")

    catalog_by_id: dict[str, dict[str, Any]] = {}
    for item in catalog:
        sop_id = item.get("sop_id")
        if not isinstance(sop_id, str) or not sop_id:
            raise AgentWorkflowError(503, "Every SOP catalog entry must have a non-empty sop_id.")
        if sop_id in catalog_by_id:
            raise AgentWorkflowError(503, f"Duplicate SOP ID in catalog: {sop_id}")
        catalog_by_id[sop_id] = item
    return catalog, catalog_by_id


def _ticket_id(ticket: dict[str, Any]) -> str:
    return str(ticket.get("ticket_id") or ticket.get("number") or "UNKNOWN_TICKET_ID")


def _make_match_prompt(
    tickets: list[dict[str, Any]], catalog: list[dict[str, Any]], settings: Settings
) -> str:
    system_prompt = _read_prompt_preamble(
        settings.match_prompt_path, "DATA TO PROCESS", "SOP matching prompt"
    )
    return MATCH_PROMPT_TEMPLATE.format(
        system_prompt=system_prompt,
        tickets_json=json.dumps(tickets, ensure_ascii=False, indent=2),
        sop_catalog_json=json.dumps(catalog, ensure_ascii=False, indent=2),
    )


def _match_result_for_ticket(
    match_response: dict[str, Any], ticket_id: str
) -> dict[str, Any]:
    results = match_response.get("results")
    if not isinstance(results, list):
        raise AgentWorkflowError(422, "match_response must contain a results array.")
    matches = [
        item
        for item in results
        if isinstance(item, dict) and str(item.get("ticket_id")) == ticket_id
    ]
    if len(matches) != 1:
        raise AgentWorkflowError(
            422, f"Expected exactly one SOP match result for ticket {ticket_id}."
        )
    return matches[0]


def _selected_sop_ids(match_result: dict[str, Any]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for group_name in ("full_match", "partial_match"):
        matches = match_result.get(group_name)
        if not isinstance(matches, list):
            raise AgentWorkflowError(
                422, f"SOP match result field {group_name!r} must be an array."
            )
        for item in matches:
            if not isinstance(item, dict) or not isinstance(item.get("sop_id"), str):
                raise AgentWorkflowError(
                    422, f"Every entry in {group_name!r} must contain a string sop_id."
                )
            sop_id = item["sop_id"]
            if sop_id and sop_id not in seen:
                selected.append(sop_id)
                seen.add(sop_id)
    return selected


def _load_sop_content(
    sop_id: str, catalog_entry: dict[str, Any], content_dir: Path
) -> dict[str, Any]:
    if not content_dir.is_dir():
        raise AgentWorkflowError(
            409, f"Processed SOP content directory is missing: {content_dir}"
        )

    for path in sorted(content_dir.iterdir()):
        if (
            not path.is_file()
            or not path.name.startswith(f"{sop_id}_")
            or not path.name.endswith("_SOP_CONTENT.json")
        ):
            continue
        content = _read_json(path, f"Processed content for {sop_id}")
        semantic = content.get("semantic_content") if isinstance(content, dict) else None
        if not isinstance(semantic, dict) or semantic.get("sop_id") != sop_id:
            continue
        source_integrity = content.get("source_integrity")
        source_file_name = (
            source_integrity.get("file_name")
            if isinstance(source_integrity, dict)
            else None
        )
        expected_source_file_name = catalog_entry.get("source_file_name")
        if (
            expected_source_file_name
            and source_file_name
            and expected_source_file_name != source_file_name
        ):
            continue
        metadata_mismatch = any(
            catalog_entry.get(field)
            and semantic.get(field)
            and str(catalog_entry[field]) != str(semantic[field])
            for field in ("document_number", "revision")
        )
        if not metadata_mismatch:
            return content

    raise AgentWorkflowError(
        409,
        f"No processed SOP content matching catalog ID {sop_id} was found in {content_dir}.",
    )


def _make_proposal_prompts(
    tickets: list[dict[str, Any]],
    match_response: dict[str, Any],
    settings: Settings,
    catalog_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    system_prompt = _read_prompt_preamble(
        settings.proposal_prompt_path, "INPUT DATA", "SOP proposal prompt"
    )
    prompts = []
    for ticket in tickets:
        ticket_id = _ticket_id(ticket)
        match_result = _match_result_for_ticket(match_response, ticket_id)
        sop_content = []
        for sop_id in _selected_sop_ids(match_result):
            catalog_entry = catalog_by_id.get(sop_id)
            if catalog_entry is None:
                raise AgentWorkflowError(
                    422, f"SOP match result references unknown catalog ID {sop_id}."
                )
            sop_content.append(
                _load_sop_content(sop_id, catalog_entry, settings.content_dir)
            )

        prompt = PROPOSAL_PROMPT_TEMPLATE.format(
            system_prompt=system_prompt,
            ticket_json=json.dumps(ticket, ensure_ascii=False, indent=2),
            match_response_json=json.dumps(
                {"results": [match_result]}, ensure_ascii=False, indent=2
            ),
            sop_content_json=json.dumps(sop_content, ensure_ascii=False, indent=2),
        )
        prompts.append({"ticket_id": ticket_id, "prompt": prompt})
    return prompts


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings.from_environment()

    def build_match_prompt(state: AgentState) -> dict[str, Any]:
        catalog, _ = _load_catalog(config)
        return {"prompt": _make_match_prompt(state["tickets"], catalog, config)}

    def build_proposal_prompts(state: AgentState) -> dict[str, Any]:
        _, catalog_by_id = _load_catalog(config)
        return {
            "prompts": _make_proposal_prompts(
                state["tickets"], state["match_response"], config, catalog_by_id
            )
        }

    graph_builder = StateGraph(AgentState)
    graph_builder.add_node("build_match_prompt", build_match_prompt)
    graph_builder.add_node("build_proposal_prompts", build_proposal_prompts)
    graph_builder.add_conditional_edges(
        START,
        lambda state: state["phase"],
        {
            "match": "build_match_prompt",
            "proposal": "build_proposal_prompts",
        },
    )
    graph_builder.add_edge("build_match_prompt", END)
    graph_builder.add_edge("build_proposal_prompts", END)
    graph = graph_builder.compile()

    app = FastAPI(
        title="SOP Ticket Prompt Agent",
        description=(
            "Runs the local SOP ingestion preparation and builds sequential ticket "
            "prompts for manual testing in Copilot. This service does not call an LLM."
        ),
        version="1.0.0",
    )

    @app.get("/", include_in_schema=False)
    def frontend() -> FileResponse:
        return FileResponse(Path(__file__).with_name("ticket_sop_ui.html"))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "llm": "not_configured"}

    @app.post("/api/ingestion/start")
    def start_ingestion() -> dict[str, str]:
        global _INGESTION_FUTURE
        pipeline_path = Path(__file__).with_name("offline_llm_sop_pipeline.py")
        if not pipeline_path.is_file():
            raise HTTPException(status_code=500, detail=f"Ingestion script not found: {pipeline_path}")

        with _INGESTION_LOCK:
            if _INGESTION_FUTURE is not None and not _INGESTION_FUTURE.done():
                return {"status": "running", "message": "Ingestion is already running."}
            _INGESTION_FUTURE = _INGESTION_EXECUTOR.submit(
                subprocess.run,
                [sys.executable, str(pipeline_path)],
                cwd=str(pipeline_path.parent),
                capture_output=True,
                text=True,
                check=False,
            )
        return {"status": "running", "message": "Ingestion cycle started."}

    @app.get("/api/ingestion/status")
    def ingestion_status() -> dict[str, Any]:
        with _INGESTION_LOCK:
            future = _INGESTION_FUTURE
        if future is None:
            return {"status": "idle"}
        if not future.done():
            return {"status": "running"}

        try:
            result = future.result()
        except OSError as exc:
            return {"status": "failed", "error": f"Could not start ingestion: {exc}"}
        return {
            "status": "completed" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    @app.post("/agent/match-prompt")
    def match_prompt(request: MatchPromptRequest) -> dict[str, str]:
        try:
            result = graph.invoke(
                {"phase": "match", "tickets": request.tickets}
            )
        except AgentWorkflowError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return {"prompt": result["prompt"]}

    @app.post("/agent/proposal-prompts")
    def proposal_prompts(request: ProposalPromptRequest) -> dict[str, Any]:
        try:
            result = graph.invoke(
                {
                    "phase": "proposal",
                    "tickets": request.tickets,
                    "match_response": request.match_response,
                }
            )
        except AgentWorkflowError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return {"prompts": result["prompts"]}

    return app


app = create_app()
