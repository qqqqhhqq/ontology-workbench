#!/usr/bin/env python3
"""便捷启动入口：运行 Operational Ontology MCP Server（JSON-RPC 2.0 over stdio）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from mcp_server.server import main

if __name__ == "__main__":
    main()
