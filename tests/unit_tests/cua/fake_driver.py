"""Subprocess MCP server for protocol tests; never accesses a real desktop."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("fake-cua-driver")
sessions: set[str] = set()


@server.tool()
def start_session(session: str) -> dict:
    sessions.add(session)
    return {"started": session}


@server.tool()
def end_session(session: str) -> dict:
    sessions.remove(session)
    return {"ended": session}


@server.tool()
def list_windows(session: str) -> dict:
    if session not in sessions:
        raise ValueError("session missing")
    return {"windows": [{"pid": 42, "window_id": 7, "title": "Calculator"}]}


@server.tool()
def get_window_state(session: str) -> dict:
    if session not in sessions:
        raise ValueError("session missing")
    return {
        "bounds": {"x": 10, "y": 20},
        "element_count": 1,
        "elements": [{"element_index": 0, "text": "391"}],
    }


if __name__ == "__main__":
    server.run(transport="stdio")
