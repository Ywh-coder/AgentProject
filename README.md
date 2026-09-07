# ReAct Agent — 基于 DeepSeek 的智能工具调用助手

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)

这是一个使用 **ReAct（Reasoning + Acting）** 框架实现的轻量级智能体（Agent），它能够根据用户问题自主决定调用计算器、网络搜索等工具，最终给出答案。项目核心逻辑清晰，适合作为学习 Agent 开发的入门示例。

## 功能特点

- **工具注册机制**：通过装饰器轻松注册自定义工具。
- **内置工具**：
  - `calculator`：安全计算数学表达式（基于 `simpleeval`）。
  - `web_search`：优先使用 Bing 搜索，失败后自动回退到 DuckDuckGo，获取网页摘要。
- **ReAct 循环**：模型自主决策 → 执行工具 → 观察结果 → 迭代直至给出最终答案。
- **Token 截断**：自动管理对话历史，防止超出上下文长度。
- **日志与重试**：内置日志记录和解析重试机制，提高稳定性。

## 项目结构

\`\`\`
AgentProject/
├── agent_v2/           # Agent 核心包
│   ├── __init__.py
│   ├── agent.py        # ReAct 主循环、ToolRegistry、提示词构建
│   └── tools.py        # 工具实现（calculator、web_search）
├── main.py             # 命令行入口
├── requirements.txt
└── README.md
\`\`\`

## 安装与配置

### 1. 克隆仓库

\`\`\`bash
git clone https://github.com/Ywh-coder/AgentProject.git
cd AgentProject
python -m venv venv
venv\Scripts\activate    # Windows
# source venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
\`\`\`

### 2. 配置 API Key

在项目根目录创建 `.env` 文件：

\`\`\`env
DEEPSEEK_API_KEY=your_deepseek_api_key_here
\`\`\`

> API Key 可在 [DeepSeek 平台](https://platform.deepseek.com/) 获取。

## 使用方法

### 命令行交互

\`\`\`bash
python main.py
\`\`\`

按提示输入问题，输入 `exit` 退出。

### 作为模块导入

\`\`\`python
from agent_v2 import react_agent

result = react_agent("北京今天天气怎么样？", verbose=True)
print(result)
\`\`\`

## 扩展自定义工具

在 `agent_v2/tools.py` 中添加新工具，然后通过装饰器注册：

\`\`\`python
from agent_v2.agent import registry

@registry.register(
    name="my_tool",
    description="我的自定义工具描述",
    parameters={"input": "str"}
)
def my_tool(input: str) -> str:
    return f"处理结果: {input}"
\`\`\`

## 技术栈

- **LLM**：DeepSeek Chat（通过 OpenAI 兼容接口）
- **搜索**：Bing Search + DuckDuckGo 双引擎
- **安全计算**：simpleeval（沙箱数学表达式求值）
- **Token 估算**：tiktoken

## License

MIT License