import sys
import json
import os
import re
import logging
from typing import Dict, Any, List, Optional, Callable
import locale

# ---------- 环境与日志配置 ----------
try:
    locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')
except locale.Error:
    pass

logging.basicConfig(
    level=os.getenv(\"LOG_LEVEL\", \"INFO\").upper(),
    format=\"%(asctime)s - %(name)s - %(levelname)s - %(message)s\"
)
logger = logging.getLogger(\"ReActAgent\")

from dotenv import load_dotenv
load_dotenv()

DEEPSEEK_API_KEY = os.getenv(\"DEEPSEEK_API_KEY\")
if not DEEPSEEK_API_KEY:
    raise RuntimeError(\"环境变量 DEEPSEEK_API_KEY 未设置，请在 .env 文件中配置。\")

from openai import OpenAI
client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=\"https://api.deepseek.com/v1\",
)

from .tools import calculator, web_search


# ---------- 工具注册与装饰器 ----------
class ToolRegistry:
    \"\"\"工具注册表，管理所有可用工具\"\"\"
    def __init__(self):
        self._tools: Dict[str, Dict] = {}

    def register(self, name: str, description: str, parameters: Dict[str, str]):
        \"\"\"装饰器：注册工具\"\"\"
        def decorator(func: Callable):
            self._tools[name] = {
                \"name\": name,
                \"description\": description,
                \"parameters\": parameters,
                \"function\": func,
            }
            return func
        return decorator

    def get_all(self) -> List[Dict]:
        \"\"\"返回所有工具描述（用于提示词）\"\"\"
        return [
            {
                \"name\": t[\"name\"],
                \"description\": t[\"description\"],
                \"parameters\": t[\"parameters\"],
            }
            for t in self._tools.values()
        ]

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        \"\"\"执行工具，返回观察结果字符串\"\"\"
        tool = self._tools.get(name)
        if not tool:
            return f\"未知工具: {name}\"
        try:
            result = tool[\"function\"](**args)
            return str(result)
        except TypeError as e:
            return f\"参数错误: {e}\"
        except Exception as e:
            logger.exception(f\"工具 {name} 执行异常\")
            return f\"工具执行异常: {e}\"

    def get_tools_description(self) -> str:
        \"\"\"生成工具描述文本\"\"\"
        lines = []
        for t in self._tools.values():
            params = \", \".join(f\"{k}: {v}\" for k, v in t[\"parameters\"].items())
            lines.append(f\"- {t['name']}({params}): {t['description']}\")
        return chr(10).join(lines)


# 创建全局注册表
registry = ToolRegistry()

# 注册内置工具
registry.register(
    name=\"calculator\",
    description=\"安全计算数学表达式，例如 '2+3*4'\",
    parameters={\"expression\": \"str\"},
)(calculator)

registry.register(
    name=\"web_search\",
    description=\"搜索互联网获取最新信息，返回相关网页摘要\",
    parameters={\"query\": \"str\"},
)(web_search)


# ---------- 提示词构建 ----------
SYSTEM_PROMPT_TEMPLATE = \"\"\"你是一个智能助手，可以使用以下工具：
{tools_desc}

请根据用户问题选择适当的工具，或直接回答。
输出格式要求：
- 如果使用工具，输出 JSON：{{\"action\": \"工具名\", \"action_input\": {{\"参数名\": \"参数值\"}}}}
- 如果已经得到最终答案，输出 JSON：{{\"action\": \"final\", \"action_input\": \"最终答案\"}}
注意：只能输出一个 JSON 对象，不要输出其他内容。\"\"\"

def build_system_prompt(tools_desc: str) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(tools_desc=tools_desc)


# ---------- Token 截断 ----------
try:
    import tiktoken
    ENCODING = tiktoken.encoding_for_model(\"gpt-4\")

    def count_tokens(text: str) -> int:
        return len(ENCODING.encode(text))
except ImportError:
    logger.warning(\"tiktoken 未安装，使用字符数估算 token（1 token≈4 字符）\")
    def count_tokens(text: str) -> int:
        return len(text) // 4


def truncate_messages(messages: List[Dict[str, str]], max_tokens: int = 3000) -> List[Dict[str, str]]:
    \"\"\"智能截断：保留系统消息和最新的 N 轮对话\"\"\"
    total = sum(count_tokens(m.get(\"content\", \"\")) for m in messages)
    if total <= max_tokens:
        return messages

    while len(messages) > 1:
        removed = messages.pop(1)
        total -= count_tokens(removed.get(\"content\", \"\"))
        if total <= max_tokens:
            break
    else:
        if total > max_tokens:
            sys_content = messages[0][\"content\"]
            messages[0][\"content\"] = sys_content[:max_tokens * 4]
    return messages


# ---------- LLM 调用 ----------
def call_llm(messages: List[Dict[str, str]], temperature: float = 0.0) -> Optional[str]:
    try:
        response = client.chat.completions.create(
            model=\"deepseek-chat\",
            messages=messages,
            temperature=temperature,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f\"LLM 调用失败: {e}\")
        return None


# ---------- 解析模型输出 ----------
def parse_action(text: str) -> tuple:
    \"\"\"从模型输出中提取 action 和 action_input\"\"\"
    text = re.sub(r'`json\s*|`\s*', '', text).strip()

    start = text.find('{')
    if start == -1:
        raise ValueError(\"未找到 JSON 起始符 '{'\")

    brace_count = 0
    end = start
    for i, ch in enumerate(text[start:], start):
        if ch == '{':
            brace_count += 1
        elif ch == '}':
            brace_count -= 1
            if brace_count == 0:
                end = i
                break
    if end == start:
        raise ValueError(\"未找到匹配的 JSON 结束符 '}'\")
    json_str = text[start:end+1]

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f\"JSON 解析失败: {e}\")

    action = data.get(\"action\")
    action_input = data.get(\"action_input\")
    if action is None:
        raise ValueError(\"JSON 缺少 'action' 字段\")
    return action, action_input


# ---------- ReAct 主循环 ----------
def react_agent(
    user_query: str,
    max_steps: int = 5,
    max_parse_retries: int = 3,
    verbose: bool = True,
) -> str:
    \"\"\"ReAct 智能体主循环\"\"\"
    tools_desc = registry.get_tools_description()
    system_prompt = build_system_prompt(tools_desc)

    messages = [
        {\"role\": \"system\", \"content\": system_prompt},
        {\"role\": \"user\", \"content\": user_query},
    ]

    for step in range(max_steps):
        if verbose:
            logger.info(f\"--- 步骤 {step+1} ---\")

        response = call_llm(messages)
        if not response:
            return \"LLM 调用失败，任务中止。\"

        if verbose:
            logger.info(f\"模型输出: {response}\")

        parse_ok = False
        for retry in range(max_parse_retries):
            try:
                action, action_input = parse_action(response)
                parse_ok = True
                break
            except Exception as e:
                error_msg = f\"输出格式错误：{e}，请重新按 JSON 格式输出。\"
                messages.append({\"role\": \"assistant\", \"content\": response})
                messages.append({\"role\": \"user\", \"content\": error_msg})
                if verbose:
                    logger.warning(f\"解析失败 (尝试 {retry+1}/{max_parse_retries}): {e}\")
                response = call_llm(messages)
                if not response:
                    return \"LLM 调用失败，任务中止。\"
                continue

        if not parse_ok:
            return \"解析模型输出超过重试次数，任务失败。\"

        if verbose:
            logger.info(f\"动作: {action}, 输入: {action_input}\")

        if action == \"final\":
            final_answer = action_input if isinstance(action_input, str) else json.dumps(action_input, ensure_ascii=False)
            return final_answer

        if not isinstance(action_input, dict):
            tool_info = next((t for t in registry.get_all() if t[\"name\"] == action), None)
            if tool_info:
                param_names = list(tool_info[\"parameters\"].keys())
                if len(param_names) == 1:
                    action_input = {param_names[0]: action_input}
                else:
                    return f\"工具 {action} 需要多个参数，但输入不是字典。\"
            else:
                return f\"未知工具 {action}\"

        observation = registry.execute(action, action_input)
        if verbose:
            logger.info(f\"观察: {observation}\")

        messages.append({\"role\": \"assistant\", \"content\": response})
        messages.append({\"role\": \"user\", \"content\": f\"Observation: {observation}\"})
        messages = truncate_messages(messages, max_tokens=3000)

    return \"达到最大步数，任务未完成。\"


# ---------- 主入口 ----------
def main():
    print(\"=== ReAct Agent 演示 ===\")
    print(\"支持的工具：\")
    for tool in registry.get_all():
        print(f\"  - {tool['name']}: {tool['description']}\")

    while True:
        try:
            user_input = input(\"\\n请输入问题（输入 exit 退出）: \").strip()
        except (KeyboardInterrupt, EOFError):
            print(\"\\n退出程序。\")
            break

        if user_input.lower() == \"exit\":
            break
        if not user_input:
            continue

        result = react_agent(user_input, verbose=True)
        print(f\"\\n最终答案: {result}\")
