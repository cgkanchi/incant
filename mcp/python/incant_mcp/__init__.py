"""incant-mcp — MCP server for Incant. Run `incant-mcp` with INCANT_URL and
INCANT_API_KEY set; add --read-only to register only the read/test tools."""

from .server import create_server, main

__all__ = ["create_server", "main"]
__version__ = "1.0.0"
