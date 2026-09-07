#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ReAct Agent 命令行入口"""

import sys
import os

# 确保项目根目录在 Python 路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_v2 import main

if __name__ == "__main__":
    main()
