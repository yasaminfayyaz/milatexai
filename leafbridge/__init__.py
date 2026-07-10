"""LeafBridge — a local/remote MCP server for editing Overleaf projects over Git.

Phase 1 (this code): a local MCP-over-HTTP server that clones your Overleaf
project via its Git bridge and exposes read / edit / write tools to Claude and
ChatGPT. No auth, no billing — you supply your project link and Git token in a
local ``projects.json`` and edit your paper straight from the AI.
"""

__version__ = "0.1.0"
