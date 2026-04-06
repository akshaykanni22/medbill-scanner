"""
backend/mcp_server.py
============================================================
PURPOSE:
    MCP (Model Context Protocol) server exposing 3 tools for
    interacting with the MedBill Scanner from an MCP client
    (e.g., Claude Desktop, Claude Code).

    Implements the MCP JSON-RPC 2.0 protocol over stdio transport,
    using only Python standard library + approved project dependencies
    (no external MCP SDK required).

TOOLS EXPOSED:
    1. lookup_hcpcs_code        — exact HCPCS/CPT code lookup
    2. search_hcpcs             — semantic search by description
    3. analyze_bill_text        — run full anomaly detection on redacted text

MCP PROTOCOL OVERVIEW:
    - Transport: stdio (read from stdin, write to stdout)
    - Format: JSON-RPC 2.0 (https://www.jsonrpc.org/specification)
    - Each message is a newline-delimited JSON object
    - Server responds to: initialize, tools/list, tools/call

HOW TO RUN:
    python -m backend.mcp_server

    Or configure in Claude Desktop's settings.json:
    {
      "mcpServers": {
        "medbill": {
          "command": "python",
          "args": ["-m", "backend.mcp_server"],
          "cwd": "/Volumes/Sam-mini-extra/projects/medbill-scanner"
        }
      }
    }

SECURITY NOTES:
    - analyze_bill_text accepts ONLY pre-redacted text.
      This tool is intended for development/testing workflows
      where the caller has already run PII redaction.
      It runs assert_no_pii_leak() as a final safety check.
    - No file uploads are accepted — this is a text-only MCP interface.
    - ChromaDB queries run locally; no patient data leaves Docker.
============================================================
"""

import asyncio
import json
import logging
import sys
from typing import Any

from backend.api.models import RAGResult, RedactedBill
from backend.rag import retriever
from backend.services import anomaly_detector, dispute_generator, pii_redactor

log = logging.getLogger(__name__)

# ============================================================
# MCP TOOL DEFINITIONS
# ============================================================

_TOOLS = [
    {
        "name": "lookup_hcpcs_code",
        "description": (
            "Look up a specific HCPCS or CPT code in the Medicare reference database. "
            "Returns the code description, Medicare reference price (USD), and "
            "total Relative Value Units (RVUs). Use this when you have an exact code "
            "from a medical bill and want to know the fair Medicare price."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "The HCPCS or CPT code to look up. "
                        "Examples: '99213' (office visit), 'J0696' (drug code). "
                        "Case insensitive."
                    ),
                }
            },
            "required": ["code"],
        },
    },
    {
        "name": "search_hcpcs",
        "description": (
            "Search the HCPCS/CPT code database by natural language description "
            "or partial code. Returns the top matching codes with Medicare "
            "reference prices. Use this when you have a procedure description "
            "from a bill but are not sure of the exact code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A procedure description or code fragment to search for. "
                        "Examples: 'office visit established patient', "
                        "'blood glucose test', 'chest X-ray'."
                    ),
                },
                "n_results": {
                    "type": "integer",
                    "description": "Number of results to return. Default 5, max 10.",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "analyze_bill_text",
        "description": (
            "Analyze pre-redacted medical bill text for billing anomalies. "
            "Runs the full MedBill Scanner pipeline: RAG price lookup + "
            "ReAct agent analysis + dispute letter generation. "
            "IMPORTANT: The text MUST have patient PII already removed before "
            "calling this tool. The tool will refuse if PII patterns are detected."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "redacted_text": {
                    "type": "string",
                    "description": (
                        "Medical bill text with all patient PII already removed. "
                        "Must contain billing codes and amounts. "
                        "Minimum 50 characters."
                    ),
                },
                "filename": {
                    "type": "string",
                    "description": "Optional original filename for logging purposes.",
                    "default": "mcp_input",
                },
            },
            "required": ["redacted_text"],
        },
    },
]


# ============================================================
# TOOL IMPLEMENTATIONS
# ============================================================

def _tool_lookup_hcpcs_code(params: dict[str, Any]) -> str:
    """
    Exact HCPCS code lookup via the RAG retriever.

    Returns a JSON string with the code details, or an error message.
    """
    code = str(params.get("code", "")).strip().upper()
    if not code:
        return json.dumps({"error": "code parameter is required"})

    try:
        result = retriever.lookup_by_code(code)
    except Exception as exc:
        log.error(f"MCP lookup_hcpcs_code failed for '{code}': {exc}")
        return json.dumps({"error": f"Database lookup failed: {exc}"})

    if result is None:
        return json.dumps({
            "found": False,
            "code": code,
            "message": f"Code '{code}' not found in the HCPCS database.",
        })

    return json.dumps({
        "found": True,
        **result,
    })


def _tool_search_hcpcs(params: dict[str, Any]) -> str:
    """
    Semantic HCPCS search via the RAG retriever.

    Returns a JSON string with up to n_results matching codes.
    """
    query = str(params.get("query", "")).strip()
    if not query:
        return json.dumps({"error": "query parameter is required"})

    n_results = int(params.get("n_results", 5))
    n_results = max(1, min(10, n_results))  # clamp to [1, 10]

    try:
        results = retriever.search(query=query, n_results=n_results)
    except Exception as exc:
        log.error(f"MCP search_hcpcs failed for query '{query}': {exc}")
        return json.dumps({"error": f"Search failed: {exc}"})

    return json.dumps({
        "query": query,
        "results": results,
        "count": len(results),
    })


async def _tool_analyze_bill_text(params: dict[str, Any]) -> str:
    """
    Full bill analysis pipeline on pre-redacted text.

    SECURITY:
        Runs assert_no_pii_leak() before passing text to the anomaly
        detector (which calls the Anthropic API). Returns an error
        if PII patterns are detected.

    Returns a JSON string with anomalies, bill_summary, and dispute_letter.
    """
    redacted_text = str(params.get("redacted_text", "")).strip()
    filename = str(params.get("filename", "mcp_input"))

    if not redacted_text:
        return json.dumps({"error": "redacted_text parameter is required"})

    # Belt-and-suspenders PII check: caller claims text is redacted,
    # but we verify before it reaches the Anthropic API.
    # Use a short dummy "original" — we don't have the raw text here,
    # so we check if the text itself contains our PII patterns.
    if not pii_redactor.assert_no_pii_leak(redacted_text, redacted_text):
        return json.dumps({
            "error": "pii_detected",
            "message": (
                "The provided text appears to contain patient PII. "
                "Please run PII redaction before calling this tool."
            ),
        })

    redacted_bill = RedactedBill(
        redacted_text=redacted_text,
        original_filename=filename,
        file_type="pdf",  # MCP callers provide text — treat as PDF-extracted
        char_count=len(redacted_text),
    )

    try:
        anomalies, bill_summary = await anomaly_detector.detect_anomalies(redacted_bill)
    except ValueError as exc:
        return json.dumps({"error": "bill_too_short", "message": str(exc)})
    except Exception as exc:
        log.error(f"MCP analyze_bill_text failed: {exc}", exc_info=True)
        return json.dumps({"error": "analysis_failed", "message": str(exc)})

    dispute_letter = None
    if anomalies:
        try:
            letter = await dispute_generator.generate(
                anomalies=anomalies,
                bill_summary=bill_summary,
            )
            dispute_letter = letter.model_dump()
        except Exception as exc:
            log.error(f"MCP dispute generation failed: {exc}")
            # Non-fatal — return anomalies without letter

    return json.dumps({
        "anomaly_count": bill_summary.anomaly_count,
        "high_severity_count": bill_summary.high_severity_count,
        "medium_severity_count": bill_summary.medium_severity_count,
        "potential_overcharge_total": bill_summary.potential_overcharge_total,
        "total_line_items": bill_summary.total_line_items,
        "anomalies": [a.model_dump() for a in anomalies],
        "dispute_letter": dispute_letter,
    }, default=str)


# ============================================================
# JSON-RPC 2.0 HANDLER
# ============================================================

async def _handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """
    Route a JSON-RPC 2.0 request to the appropriate handler.

    WHAT:
        Dispatches based on request["method"]:
        - "initialize"    → return server capabilities
        - "tools/list"    → return the list of available tools
        - "tools/call"    → call a named tool and return its result

    WHY ASYNC:
        tools/call may invoke analyze_bill_text which is async
        (it awaits Anthropic API calls). The entire handler must
        be async to support this.

    RETURNS:
        A JSON-RPC 2.0 response dict (always has "id" and either
        "result" or "error").
    """
    req_id = request.get("id")
    method = request.get("method", "")
    params = request.get("params", {})

    def ok(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }

    # --- initialize ---
    if method == "initialize":
        return ok({
            "protocolVersion": "2024-11-05",
            "serverInfo": {
                "name": "medbill-scanner",
                "version": "0.1.0",
            },
            "capabilities": {
                "tools": {},
            },
        })

    # --- initialized notification (no response needed) ---
    if method == "notifications/initialized":
        return None  # type: ignore[return-value]

    # --- tools/list ---
    if method == "tools/list":
        return ok({"tools": _TOOLS})

    # --- tools/call ---
    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_input = params.get("arguments", {})

        if tool_name == "lookup_hcpcs_code":
            content = _tool_lookup_hcpcs_code(tool_input)
        elif tool_name == "search_hcpcs":
            content = _tool_search_hcpcs(tool_input)
        elif tool_name == "analyze_bill_text":
            content = await _tool_analyze_bill_text(tool_input)
        else:
            return err(-32601, f"Unknown tool: '{tool_name}'")

        return ok({
            "content": [{"type": "text", "text": content}],
            "isError": False,
        })

    # --- ping ---
    if method == "ping":
        return ok({})

    return err(-32601, f"Method not found: '{method}'")


# ============================================================
# STDIO TRANSPORT
# ============================================================

async def run_stdio() -> None:
    """
    Run the MCP server, reading JSON-RPC messages from stdin
    and writing responses to stdout.

    WHAT:
        - Reads one JSON object per line from stdin.
        - Parses as JSON-RPC 2.0.
        - Calls _handle_request() for each message.
        - Writes the response as a single JSON line to stdout.
        - Runs until stdin closes (EOF).

    WHY NEWLINE-DELIMITED JSON:
        The MCP spec uses newline-delimited JSON over stdio.
        Each message is exactly one line. This is the simplest
        framing that both the server and client can implement
        without a length-prefix or other header.

    WHY asyncio (not threading):
        analyze_bill_text calls the Anthropic API asynchronously.
        The event loop handles this naturally. A threaded server
        would need explicit synchronisation around the ChromaDB
        singleton and asyncio event loop.
    """
    log.info("MedBill MCP server started (stdio transport)")

    loop = asyncio.get_event_loop()

    # Set up async stdin reader.
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    # Write to stdout in a thread-safe way.
    # WHY sys.stdout.buffer: MCP messages are UTF-8 text. Writing to
    # the binary buffer and manually encoding avoids platform line-ending
    # translation on Windows (not our target, but good practice).
    def write_response(obj: dict) -> None:
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        sys.stdout.buffer.write(line.encode("utf-8"))
        sys.stdout.buffer.flush()

    while True:
        try:
            line_bytes = await reader.readline()
        except (OSError, EOFError):
            break  # stdin closed or I/O error

        if not line_bytes:
            break  # EOF

        line = line_bytes.decode("utf-8").strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            write_response({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            })
            continue

        try:
            response = await _handle_request(request)
            if response is not None:  # notifications have no response
                write_response(response)
        except Exception as exc:
            log.error(f"Unhandled error processing request: {exc}", exc_info=True)
            write_response({
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            })


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        stream=sys.stderr,  # WHY stderr: stdout is reserved for MCP JSON messages
    )
    asyncio.run(run_stdio())
