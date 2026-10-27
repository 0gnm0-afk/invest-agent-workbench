"""Stable registered STDIO MCP command; tools live in analysis.mcp."""
from analysis.mcp import mcp

if __name__ == '__main__':
    mcp.run(transport='stdio')
