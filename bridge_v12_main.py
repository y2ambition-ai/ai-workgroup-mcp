#!/usr/bin/env python3
"""CCBridge v12 主入口"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from bridge_v12.tools import mcp

if __name__ == "__main__":
    mcp.run()
