import sys
import json
import os
import re
import logging
from typing import Dict, Any, List, Optional, Callable
import locale
import time

# ---------- 环境与日志配置 ----------
try:
    locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')
except locale.Error:
    pass

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("ReActAgent")

from dotenv import load_dotenv
load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise RuntimeError("环境变量 DEEPSEEK_API_KEY 未设置，请在 .env 文件中配置。")

from openai import OpenAI
client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1",
)

from .tools import calculator, web_search, TOOLS_SCHEMA



_TOOL_MAX_FAILURES = 3
# ---------- Circuit Breaker ----------
class CircuitBreaker:
    def __init__(self, max_failures=_TOOL_MAX_FAILURES):
        self._failures: Dict[str, int] = {}
        self._max = max_failures
    def is_open(self, name: str) -> bool:
        return self._failures.get(name, 0) >= self._max
    def record(self, name: str) -> None:
        self._failures[name] = self._failures.get(name, 0) + 1
    def reset(self, name: str) -> None:
        self._failures.pop(name, None)
    def get_stats(self) -> Dict[str, Dict]:
        return {name: {"failure_count": count, "circuit_open": count >= self._max} for name, count in self._failures.items()}


circuit_breaker = CircuitBreaker()

# ---------- Tool Registry ----------
class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Dict] = {}
        self._stats: Dict[str, Dict[str, int]] = {"calls": {}, "success": {}, "failure": {}}
    def register(self, name: str, description: str, parameters: Dict[str, Any]):
        def decorator(func: Callable):
            self._tools[name] = {"name": name, "description": description, "parameters": parameters, "function": func}
            return func
        return decorator
    def get_all(self) -> List[Dict]:
        return [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]} for t in self._tools.values()]
    def execute(self, name: str, args: Dict[str, Any]) -> str:
        if circuit_breaker.is_open(name):
            return f"Circuit open for tool {name}: too many consecutive failures. Aborting."
        tool = self._tools.get(name)
        if not tool:
            return f"Unknown tool: {name}"
        self._stats["calls"][name] = self._stats["calls"].get(name, 0) + 1
        try:
            result = tool["function"](**args)
            self._stats["success"][name] = self._stats["success"].get(name, 0) + 1
            circuit_breaker.reset(name)
            return str(result)
        except Exception as e:
            self._stats["failure"][name] = self._stats["failure"].get(name, 0) + 1
            circuit_breaker.record(name)
            logger.exception(f"Tool {name} execution failed")
            return f"Tool error [{name}]: {e}"
    def get_tools_json_schema(self) -> List[Dict]:
        return TOOLS_SCHEMA
    def get_tools_text_description(self) -> str:
        """Format tool descriptions as human-readable text from JSON Schema."""
        result = []
        for t in self._tools.values():
            schema = t["parameters"]
            props = schema.get("properties", {})
            param_strs = []
            for pname, pspec in props.items():
                ptype = pspec.get("type", "str")
                pdesc = pspec.get("description", "")
                if pdesc:
                    param_strs.append(f"{pname} ({ptype}): {pdesc[:60]}")
                else:
                    param_strs.append(f"{pname} ({ptype})")
            params = ", ".join(param_strs) if param_strs else ""
            result.append(f"- {t["name"]}({params}): {t["description"]}")
        return chr(10).join(result)

registry = ToolRegistry()
registry.register(name="calculator", description=TOOLS_SCHEMA[0]["description"], parameters=TOOLS_SCHEMA[0]["parameters"])(calculator)
registry.register(name="web_search", description=TOOLS_SCHEMA[1]["description"], parameters=TOOLS_SCHEMA[1]["parameters"])(web_search)

# ---------- System Prompt ----------
SYSTEM_PROMPT_TEMPLATE = """You are a helpful assistant that can use tools to answer questions.

Available tools:
{tools_desc}

Output format (one JSON per response):
{{"thought": "your step-by-step reasoning", "action": "tool_name", "action_input": {{...}}}}
When done: {{"thought": "summary of reasoning", "final_answer": "your answer"}}

Rules:
- Use ONLY the tools listed above
- "thought" must explain WHY you choose this action and what you expect
- If a tool failed before, explain what you'll do differently next time
- Do not repeat the same failing action more than once
- Always think step by step before acting
- Output exactly one JSON block per response"""

def build_system_prompt(tools_desc: str) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(tools_desc=tools_desc)
# ---------- Token 截断 ----------
try:
    import tiktoken
    ENCODING = tiktoken.encoding_for_model("gpt-4")

    def count_tokens(text: str) -> int:
        return len(ENCODING.encode(text))
except ImportError:
    logger.warning("tiktoken 未安装，使用字符数估算 token（1 token≈4 字符）")
    def count_tokens(text: str) -> int:
        return len(text) // 4



def truncate_messages(messages: List[Dict[str, str]], max_tokens: int = 3000) -> List[Dict[str, str]]:
    """Trim old conversation turns (keeping system prompt) to fit within max_tokens.
    Removes complete assistant+observation pairs from the end."""
    total = sum(count_tokens(m.get("content", "")) for m in messages)
    if total <= max_tokens:
        return messages
    # Group messages into turns: first msg is system, then pairs of (assistant, observation)
    turns = []  # list of [assistant_msg, observation_msg]
    i = 1  # skip system prompt at index 0
    while i < len(messages):
        if messages[i].get("role") == "assistant" and i + 1 < len(messages):
            turns.append([messages[i], messages[i + 1]])
            i += 2
        else:
            # Orphaned message (no matching pair), skip it
            i += 1
    # Remove turns from the end until under safe limit (留出 20% buffer)
    safe_limit = int(max_tokens * 0.8)
    while turns and total > safe_limit:
        turn = turns.pop()
        total -= count_tokens(turn[0].get("content", ""))
        total -= count_tokens(turn[1].get("content", ""))
    # Rebuild: system + remaining complete turns only
    result = [messages[0]] if messages else []
    for turn in turns:
        result.extend(turn)
    return result
# ---------- LLM 调用 ----------
# ---------- LLM Call with timeout and retry ----------
def call_llm(messages: List[Dict[str, str]], temperature: float = 0.0, max_retries: int = 3) -> Optional[str]:
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                temperature=temperature,
                timeout=30.0,
            )
            return response.choices[0].message.content
        except Exception as e:
            err_str = str(e)
            if "401" in err_str or "400" in err_str or "invalid" in err_str.lower():
                logger.error(f"LLM non-retryable error: {e}")
                return None
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"LLM call failed (attempt {attempt+1}/{max_retries}): {e}, retrying in {wait}s")
                time.sleep(wait)
            else:
                logger.error(f"LLM call failed after {max_retries} attempts: {e}")
                return None
# ---------- 解析模型输出 ----------
def parse_action(text: str) -> tuple:
    """从模型输出中提取 action 和 action_input"""
    text = re.sub(r'`json\s*|`\s*', '', text).strip()

    start = text.find('{')
    if start == -1:
        raise ValueError("未找到 JSON 起始符 '{'")

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
        raise ValueError("未找到匹配的 JSON 结束符 '}'")
    json_str = text[start:end+1]

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}")

    action = data.get("action")
    action_input = data.get("action_input")
    final_answer = data.get("final_answer")
    if action is not None:
        return action, action_input
    elif final_answer is not None:
        return "final", final_answer
    raise ValueError("JSON missing action or final_answer field")
# ---------- ReAct Loop with multi-turn support ----------
def react_agent(user_query: str, max_steps: int = 5, max_parse_retries: int = 3, verbose: bool = True, conversation_history: Optional[List[Dict]] = None) -> tuple:
    tools_desc = registry.get_tools_text_description()
    system_prompt = build_system_prompt(tools_desc)
    messages = [
        {"role": "system", "content": system_prompt},
    ]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_query})
    for step in range(max_steps):
        if verbose:
            logger.info(f"--- Step {step+1} ---")
        response = call_llm(messages)
        if not response:
            return None, "LLM call failed, stopping."
        if verbose:
            logger.info(f"Model response: {response}")
        # Extract thought from response
        thought = ""
        clean_text = re.sub(r"```json\s*|```\s*", "", response).strip()
        start = clean_text.find("{")
        if start != -1:
            thought_match = re.search(r"\"thought\":\s*\"([^\"]+)\"", clean_text[start:])
            if thought_match:
                thought = thought_match.group(1)
        parse_ok = False
        parse_messages = list(messages)  # snapshot for parse retries (isolated from main messages)
        for retry in range(max_parse_retries):
            try:
                action, action_input = parse_action(response)
                parse_ok = True
                break
            except Exception as e:
                error_msg = f"JSON parse error: {e}. Please output valid JSON with thought/action/final_answer fields."
                # Replace last assistant entry (if any) + append fresh response + error
                if parse_messages and parse_messages[-1]["role"] == "assistant":
                    parse_messages.pop()
                parse_messages.append({"role": "assistant", "content": response})
                parse_messages.append({"role": "user", "content": error_msg})
                if verbose:
                    logger.warning(f"Parse failed (retry {retry+1}/{max_parse_retries}): {e}")
                response = call_llm(parse_messages, temperature=0.3)
                if not response:
                    return None, "LLM call failed during retry, stopping."
                continue
        if not parse_ok:
            return None, "Model could not produce valid JSON after retries."
        if verbose:
            logger.info(f"Action: {action}, Input: {action_input}, Thought: {thought}")
        if action == "final_answer" or action == "final":
            final_answer = action_input if isinstance(action_input, str) else json.dumps(action_input, ensure_ascii=False)
            messages.append({"role": "assistant", "content": response})
            return messages, final_answer
        if not isinstance(action_input, dict):
            tool_info = next((t for t in registry.get_all() if t["name"] == action), None)
            if tool_info:
                param_names = list(tool_info["parameters"].keys())
                if len(param_names) == 1:
                    action_input = {param_names[0]: action_input}
                else:
                    return f"Tool {action} requires named parameters, got non-dict input."
            else:
                return f"Unknown tool: {action}"
        observation = registry.execute(action, action_input)
        if verbose:
            logger.info(f"Observation: {observation}")
        messages.append({"role": "assistant", "content": response})
        if thought:
            observation_msg = f"[Reasoning: {thought}]\nObservation: {observation}"
        else:
            observation_msg = f"Observation: {observation}"
        messages.append({"role": "user", "content": observation_msg})
        messages = truncate_messages(messages, max_tokens=3000)
    return messages, "Reached max steps without completing."

# ---------- Main (multi-turn) ----------
def main():
    print("=== ReAct Agent v2 ===")
    print("Available tools:")
    for tool in registry.get_all():
        print(f"  - {tool['name']}: {tool['description']}")
    print(f"Tool stats: {circuit_breaker.get_stats()}")
    conversation_history = []
    while True:
        try:
            user_input = input('\n\n>>> input prompt: ').strip()
        except (KeyboardInterrupt, EOFError):
            print('\n\n Stopping.')
            break
        if user_input.lower() == "exit":
            break
        if not user_input:
            continue
        updated_messages, result = react_agent(user_input, verbose=True, conversation_history=conversation_history)
        if updated_messages:
            conversation_history = [m for m in updated_messages if m["role"] != "system"]
            # Save to conversation history for multi-turn
            conversation_history.append({"role": "user", "content": user_input})
            conversation_history.append({"role": "assistant", "content": result})
        print("\nFinal Answer: " + result)
        # Keep history within token limit
        conversation_history = truncate_messages([{"role": "system", "content": ""}] + conversation_history, max_tokens=2000)[1:]
    print("\nGoodbye!")