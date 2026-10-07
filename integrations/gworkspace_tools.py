"""
carry-ai/integrations/gworkspace_tools.py -- Google Workspace CLI Tools
=========================================================================

Wraps the Google Workspace CLI (https://github.com/googleworkspace/cli)
as carry-ai agent tools for accessing Drive, Gmail, Sheets, Calendar,
and Docs directly from the AI assistant.

Features (from GWS CLI):
    - Dynamic command generation from Google's Discovery Service
    - Drive: list, upload, download, share files
    - Gmail: send, search, read emails
    - Sheets: read, write, append data
    - Calendar: view agenda, create events
    - Docs: document operations
    - Chat: messaging
    - 100+ predefined "agent skills" (workflow automations)
    - Schema inspection for any endpoint
    - JSON output for all commands

Command shape (gws v0.22, crates/google-workspace-cli):
    gws <service> <resource> [<sub-resource>] <method> --params '<query JSON>' --json '<body JSON>'
    e.g. gws gmail users messages list --params '{"userId":"me"}'
    Resource paths are space-separated words (never "users.messages").
    Helpers start with "+": gmail +send, sheets +append, calendar +agenda.

Architecture:
    Each tool wraps a `gws` CLI command via subprocess. The GWS binary
    must be installed and authenticated on the host. All output is JSON,
    which is parsed and returned to the agent.

Auth:
    Run `gws auth login` once to authenticate. Credentials stored at
    ~/.config/gws/. For portable USB use, point GOOGLE_WORKSPACE_CLI_
    CREDENTIALS_FILE env var to the USB path.

Reference: https://github.com/googleworkspace/cli
"""

import json
import logging
import shutil
import subprocess

log = logging.getLogger("carry-ai.integrations.gworkspace")

# Check if gws binary is available
GWS_BINARY = shutil.which("gws")
GWS_AVAILABLE = GWS_BINARY is not None


def _run_gws(args: list[str], timeout: int = 30) -> str:
    """Run a gws command and return the output."""
    if not GWS_AVAILABLE:
        return ("Error: Google Workspace CLI (gws) not found on PATH.\n"
                "Install: npm install -g @googleworkspace/cli\n"
                "  or a binary from github.com/googleworkspace/cli/releases\n"
                "Auth:  gws auth login")

    cmd = [GWS_BINARY] + args
    log.debug("Running: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            error = result.stderr.strip() or output
            return f"Error (exit {result.returncode}): {error}"
        return output or "(empty response)"
    except subprocess.TimeoutExpired:
        return f"Error: gws command timed out after {timeout}s"
    except Exception as e:
        return f"Error running gws: {e}"


def _parse_json_output(output: str) -> str:
    """Try to parse JSON output and format it nicely."""
    try:
        data = json.loads(output)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except json.JSONDecodeError:
        return output


# ===================================================================
# Tool implementations
# ===================================================================

def _tool_gdrive_list(query: str = "", max_results: int = 20) -> str:
    """List files in Google Drive.

    Args:
        query: Search query (Google Drive query syntax).
            Examples: "name contains 'report'", "mimeType = 'application/pdf'"
        max_results: Maximum number of files to return.
    """
    args = ["drive", "files", "list",
            "--params", json.dumps({
                "pageSize": min(max_results, 100),
                "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)",
            })]
    if query:
        args[-1] = json.dumps({
            "q": query,
            "pageSize": min(max_results, 100),
            "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)",
        })
    return _parse_json_output(_run_gws(args))


def _tool_gdrive_upload(file_path: str, folder_id: str = "",
                         name: str = "") -> str:
    """Upload a file to Google Drive.

    Args:
        file_path: Local file path to upload.
        folder_id: Optional Drive folder ID to upload into.
        name: Optional name for the file in Drive.
    """
    metadata: dict = {}
    if name:
        metadata["name"] = name
    if folder_id:
        metadata["parents"] = [folder_id]
    args = ["drive", "files", "create"]
    if metadata:                      # file metadata is the request body
        args.extend(["--json", json.dumps(metadata)])
    args.extend(["--upload", file_path])
    return _run_gws(args, timeout=120)


def _tool_gdrive_download(file_id: str, output_path: str = "") -> str:
    """Download a file from Google Drive.

    Args:
        file_id: Drive file ID to download.
        output_path: Local path to save the file.
    """
    # alt=media returns the file content instead of its metadata.
    args = ["drive", "files", "get",
            "--params", json.dumps({"fileId": file_id, "alt": "media"})]
    if output_path:
        args.extend(["--output", output_path])
    return _run_gws(args, timeout=120)


def _tool_gmail_search(query: str, max_results: int = 10) -> str:
    """Search Gmail messages.

    Args:
        query: Gmail search query (same as Gmail search bar).
            Examples: "from:boss subject:meeting", "is:unread", "label:inbox newer_than:1d"
        max_results: Maximum messages to return.
    """
    args = ["gmail", "users", "messages", "list",
            "--params", json.dumps({
                "userId": "me",
                "q": query,
                "maxResults": min(max_results, 50),
            })]
    return _parse_json_output(_run_gws(args))


def _tool_gmail_send(to: str, subject: str, body: str,
                      cc: str = "", bcc: str = "") -> str:
    """Send an email via Gmail.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Email body (plain text).
        cc: CC recipients (comma-separated).
        bcc: BCC recipients (comma-separated).
    """
    args = ["gmail", "+send",
            "--to", to,
            "--subject", subject,
            "--body", body]
    if cc:
        args.extend(["--cc", cc])
    if bcc:
        args.extend(["--bcc", bcc])
    return _run_gws(args)


def _tool_gmail_read(message_id: str) -> str:
    """Read a specific Gmail message by ID.

    Args:
        message_id: Gmail message ID (from search results).
    """
    args = ["gmail", "users", "messages", "get",
            "--params", json.dumps({
                "userId": "me",
                "id": message_id,
                "format": "full",
            })]
    return _parse_json_output(_run_gws(args))


def _tool_gsheets_read(spreadsheet_id: str, range_str: str = "Sheet1") -> str:
    """Read data from a Google Sheet.

    Args:
        spreadsheet_id: Spreadsheet ID (from URL).
        range_str: Cell range (e.g., "Sheet1!A1:D10", "Sheet1").
    """
    args = ["sheets", "spreadsheets", "values", "get",
            "--params", json.dumps({
                "spreadsheetId": spreadsheet_id,
                "range": range_str,
            })]
    return _parse_json_output(_run_gws(args))


def _tool_gsheets_append(spreadsheet_id: str, range_str: str,
                          values: str) -> str:
    """Append rows to a Google Sheet.

    Args:
        spreadsheet_id: Spreadsheet ID.
        range_str: Target range (e.g., "Sheet1!A:D").
        values: JSON array of arrays (rows). Example: '[["Alice",30],["Bob",25]]'
    """
    try:
        parsed_values = json.loads(values)
    except json.JSONDecodeError:
        return f"Error: values must be valid JSON array of arrays. Got: {values[:100]}"

    if not isinstance(parsed_values, list):
        return "Error: values must be a JSON array of arrays (rows)."
    args = ["sheets", "+append",
            "--spreadsheet", spreadsheet_id,
            "--json-values", json.dumps(parsed_values)]
    if range_str:
        args.extend(["--range", range_str])
    return _run_gws(args)


def _tool_gcalendar_agenda(days: int = 7, calendar_id: str = "") -> str:
    """View upcoming calendar events.

    Args:
        days: Number of days ahead to show (default 7).
        calendar_id: Calendar name or ID to filter on. Empty (or "primary",
            which +agenda can't match by name) shows all calendars.
    """
    args = ["calendar", "+agenda", "--days", str(days)]
    if calendar_id and calendar_id != "primary":
        args.extend(["--calendar", calendar_id])
    return _parse_json_output(_run_gws(args))


def _tool_gcalendar_create(summary: str, start: str, end: str,
                            description: str = "",
                            calendar_id: str = "primary") -> str:
    """Create a Google Calendar event.

    Args:
        summary: Event title.
        start: Start time (ISO 8601, e.g., "2026-04-10T10:00:00-05:00").
        end: End time (ISO 8601).
        description: Optional event description.
        calendar_id: Calendar ID (default: "primary").
    """
    event = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if description:
        event["description"] = description

    args = ["calendar", "events", "insert",
            "--params", json.dumps({"calendarId": calendar_id or "primary"}),
            "--json", json.dumps(event)]
    return _run_gws(args)


def _tool_gworkspace(service: str, method: str, params: str = "{}",
                     body: str = "") -> str:
    """Run any Google Workspace API call via the GWS CLI.

    This is a generic tool for accessing any Google Workspace API.
    Use 'gws schema <service>.<method>' to inspect available parameters.

    Args:
        service: Google service (drive, gmail, sheets, calendar, docs, chat).
        method: API method (e.g., "files.list", "users.messages.list");
            dots become the space-separated resource path gws expects.
        params: JSON string of URL/query parameters (--params).
        body: Optional JSON string request body (--json).
    """
    try:
        parsed = json.loads(params) if params else {}
    except json.JSONDecodeError:
        return f"Error: params must be valid JSON. Got: {params[:100]}"
    parsed_body = None
    if body:
        try:
            parsed_body = json.loads(body)
        except json.JSONDecodeError:
            return f"Error: body must be valid JSON. Got: {body[:100]}"

    parts = [p for p in method.replace("/", ".").split(".") if p]
    if not parts:
        return "Error: method is required (e.g. 'files.list')."
    args = [service, *parts]
    if parsed:
        args.extend(["--params", json.dumps(parsed)])
    if parsed_body is not None:
        args.extend(["--json", json.dumps(parsed_body)])
    return _parse_json_output(_run_gws(args, timeout=60))


# ===================================================================
# Registration
# ===================================================================

def register_gworkspace_tools():
    """Register Google Workspace tools in the agent tool registry."""
    from agent.tools import register_tool

    if not GWS_AVAILABLE:
        log.info("Google Workspace CLI (gws) not found -- tools will show install instructions")

    register_tool(
        name="gdrive_list",
        description="List files in Google Drive. Search with query syntax like 'name contains ...' or 'mimeType = ...'.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Drive search query", "default": ""},
                "max_results": {"type": "integer", "description": "Max files to return", "default": 20},
            },
        },
        execute_fn=_tool_gdrive_list,
    )

    register_tool(
        name="gdrive_upload",
        description="Upload a local file to Google Drive.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Local file path to upload"},
                "folder_id": {"type": "string", "description": "Optional Drive folder ID", "default": ""},
                "name": {"type": "string", "description": "Optional file name in Drive", "default": ""},
            },
            "required": ["file_path"],
        },
        execute_fn=_tool_gdrive_upload,
    )

    register_tool(
        name="gmail_search",
        description="Search Gmail messages. Uses Gmail query syntax (e.g., 'from:boss', 'is:unread', 'subject:meeting').",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "max_results": {"type": "integer", "description": "Max messages", "default": 10},
            },
            "required": ["query"],
        },
        execute_fn=_tool_gmail_search,
    )

    register_tool(
        name="gmail_send",
        description="Send an email via Gmail.",
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email"},
                "subject": {"type": "string", "description": "Subject line"},
                "body": {"type": "string", "description": "Email body (plain text)"},
                "cc": {"type": "string", "description": "CC recipients", "default": ""},
                "bcc": {"type": "string", "description": "BCC recipients", "default": ""},
            },
            "required": ["to", "subject", "body"],
        },
        execute_fn=_tool_gmail_send,
    )

    register_tool(
        name="gmail_read",
        description="Read a Gmail message by ID (get the ID from gmail_search results).",
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID"},
            },
            "required": ["message_id"],
        },
        execute_fn=_tool_gmail_read,
    )

    register_tool(
        name="gsheets_read",
        description="Read data from a Google Sheet. Specify range like 'Sheet1!A1:D10'.",
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string", "description": "Spreadsheet ID (from URL)"},
                "range_str": {"type": "string", "description": "Cell range", "default": "Sheet1"},
            },
            "required": ["spreadsheet_id"],
        },
        execute_fn=_tool_gsheets_read,
    )

    register_tool(
        name="gsheets_append",
        description="Append rows to a Google Sheet. Values as JSON array of arrays: '[[\"Alice\",30],[\"Bob\",25]]'.",
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string", "description": "Spreadsheet ID"},
                "range_str": {"type": "string", "description": "Target range (e.g., 'Sheet1!A:D')"},
                "values": {"type": "string", "description": "JSON array of row arrays"},
            },
            "required": ["spreadsheet_id", "range_str", "values"],
        },
        execute_fn=_tool_gsheets_append,
    )

    register_tool(
        name="gcalendar_agenda",
        description="View upcoming Google Calendar events for the next N days.",
        parameters={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Days ahead to show", "default": 7},
                "calendar_id": {"type": "string", "description": "Calendar name or ID (empty = all)", "default": ""},
            },
        },
        execute_fn=_tool_gcalendar_agenda,
    )

    register_tool(
        name="gcalendar_create",
        description="Create a Google Calendar event.",
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "Start time (ISO 8601)"},
                "end": {"type": "string", "description": "End time (ISO 8601)"},
                "description": {"type": "string", "description": "Event description", "default": ""},
                "calendar_id": {"type": "string", "description": "Calendar ID", "default": "primary"},
            },
            "required": ["summary", "start", "end"],
        },
        execute_fn=_tool_gcalendar_create,
    )

    register_tool(
        name="gworkspace",
        description=(
            "Run any Google Workspace API call. Generic tool for Drive, Gmail, "
            "Sheets, Calendar, Docs, Chat. Pass service name, dotted method "
            "(e.g. 'users.messages.list'), JSON query params and optional JSON body."
        ),
        parameters={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Google service (drive, gmail, sheets, calendar, docs, chat)"},
                "method": {"type": "string", "description": "API method (e.g., 'files.list')"},
                "params": {"type": "string", "description": "JSON query/path parameters", "default": "{}"},
                "body": {"type": "string", "description": "Optional JSON request body", "default": ""},
            },
            "required": ["service", "method"],
        },
        execute_fn=_tool_gworkspace,
    )

    log.info("Registered Google Workspace tools (gws_available=%s)", GWS_AVAILABLE)
