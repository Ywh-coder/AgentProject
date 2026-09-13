import sys
import json
import os
import re
import logging
from typing import Dict, Any, List, Optional, Callable
import locale
import time
import uuid

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

from .tools import calculator, web_search, send_email, TOOLS_SCHEMA



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
    def register(self, name: str, description: str, parameters: Dict[str, Any], requires_confirmation: bool = False):
        def decorator(func: Callable):
            self._tools[name] = {
                "name": name, "description": description, "parameters": parameters,
                "function": func, "requires_confirmation": requires_confirmation,
            }
            return func
        return decorator
    def get_all(self) -> List[Dict]:
        return [{"name": t["name"], "description": t["description"], "parameters": t["parameters"],
                 "requires_confirmation": t.get("requires_confirmation", False)} for t in self._tools.values()]
    def execute(self, name: str, args: Dict[str, Any], confirm_cb=None) -> str:
        """Execute a tool. If confirm_cb is provided and tool requires confirmation,
        the callback is called with (name, args) and must return True/False."""
        if circuit_breaker.is_open(name):
            return f"Circuit open for tool {name}: too many consecutive failures. Aborting."
        tool = self._tools.get(name)
        if not tool:
            return f"Unknown tool: {name}"
        # Danger confirmation
        if tool.get("requires_confirmation") and confirm_cb is not None:
            preview = json.dumps(args, ensure_ascii=False)
            confirmed = confirm_cb(name, preview)
            if not confirmed:
                return f"User cancelled tool {name}"
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
            result.append(f"- {t['name']}({params}): {t['description']}")
        return "\n".join(result)

registry = ToolRegistry()
registry.register(name="calculator", description=TOOLS_SCHEMA[0]["description"], parameters=TOOLS_SCHEMA[0]["parameters"], requires_confirmation=False)(calculator)
registry.register(name="web_search", description=TOOLS_SCHEMA[1]["description"], parameters=TOOLS_SCHEMA[1]["parameters"], requires_confirmation=False)(web_search)
registry.register(name="send_email", description=TOOLS_SCHEMA[2]["description"], parameters=TOOLS_SCHEMA[2]["parameters"], requires_confirmation=True)(send_email)

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
def react_agent(user_query: str, max_steps: int = 5, max_parse_retries: int = 3, verbose: bool = True, conversation_history: Optional[List[Dict]] = None, confirm_cb=None) -> tuple:
    """Returns (new_trace, final_answer).
    new_trace contains ONLY the assistant+observation pairs from THIS turn
    (without system prompt and without the original user_query).
    Caller appends user_query + final_answer around the trace.
    Caller is responsible for appending user_query and final_answer to history."""

    # ---------- Trace ----------
    trace_id = str(uuid.uuid4())[:8]
    trace: dict = {
        "trace_id": trace_id,
        "query": user_query,
        "conversation_history_len": len(conversation_history) if conversation_history else 0,
        "steps": [],
        "final_answer": None,
        "success": False,
        "error": None,
    }
    step_start_global = time.time()
    tools_desc = registry.get_tools_text_description()
    system_prompt = build_system_prompt(tools_desc)
    messages = [
        {"role": "system", "content": system_prompt},
    ]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_query})
    for step in range(max_steps):
        step_start = time.time()
        if verbose:
            logger.info(f"--- Step {step+1} ---")
        response = call_llm(messages)
        if not response:
            trace["error"] = "LLM call failed"
            trace["total_latency_ms"] = int((time.time() - step_start_global) * 1000)
            _save_trace(trace)
            return [], None, trace
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
                    trace["error"] = "LLM call failed during retry"
                    trace["total_latency_ms"] = int((time.time() - step_start_global) * 1000)
                    _save_trace(trace)
                    return [], None, trace
                continue
        if not parse_ok:
            trace["error"] = "Parse retry exhausted"
            trace["total_latency_ms"] = int((time.time() - step_start_global) * 1000)
            _save_trace(trace)
            return [], None, trace
        if verbose:
            logger.info(f"Action: {action}, Input: {action_input}, Thought: {thought}")
        if action == "final_answer" or action == "final":
            final_answer = action_input if isinstance(action_input, str) else json.dumps(action_input, ensure_ascii=False)
            messages.append({"role": "assistant", "content": response})
            # Extract only the assistant+observation pairs added this turn.
            # messages = [system, ...prev_history, user_query, assistant1, obs1, ...]
            n_prev = 1 + len(conversation_history) if conversation_history else 1
            new_trace = messages[n_prev + 1:]  # skip user_query, start from assistant1
            # Extract only the assistant+observation pairs added this turn.
            n_prev = 1 + len(conversation_history) if conversation_history else 1
            new_history = messages[n_prev + 1:]  # skip user_query
            trace["steps"].append({
                "step": step + 1,
                "thought": thought,
                "action": "final_answer",
                "action_input": None,
                "observation": None,
                "final_answer": final_answer,
                "latency_ms": int((time.time() - step_start) * 1000),
            })
            trace["final_answer"] = final_answer
            trace["success"] = True
            trace["total_latency_ms"] = int((time.time() - step_start_global) * 1000)
            _save_trace(trace)
            return new_history, final_answer, trace
        if not isinstance(action_input, dict):
            tool_info = next((t for t in registry.get_all() if t["name"] == action), None)
            if tool_info:
                param_names = list(tool_info["parameters"].keys())
                if len(param_names) == 1:
                    action_input = {param_names[0]: action_input}
                else:
                    trace["error"] = f"Tool {action} param error"
                    trace["total_latency_ms"] = int((time.time() - step_start_global) * 1000)
                    _save_trace(trace)
                    return [], None, trace
            else:
                trace["error"] = f"Unknown tool: {action}"
                trace["total_latency_ms"] = int((time.time() - step_start_global) * 1000)
                _save_trace(trace)
                return [], None, trace
        observation = registry.execute(action, action_input, confirm_cb=confirm_cb)
        if verbose:
            logger.info(f"Observation: {observation}")
        messages.append({"role": "assistant", "content": response})
        if thought:
            observation_msg = f"[Reasoning: {thought}]\nObservation: {observation}"
        else:
            observation_msg = f"Observation: {observation}"
        messages.append({"role": "user", "content": observation_msg})
        trace["steps"].append({
            "step": step + 1,
            "thought": thought,
            "action": action,
            "action_input": action_input,
            "observation": observation,
            "latency_ms": int((time.time() - step_start) * 1000),
        })
    trace["total_latency_ms"] = int((time.time() - step_start_global) * 1000)
    _save_trace(trace)
    return [], None, trace


def _save_trace(trace: dict) -> None:
    """Persist trace to disk."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    trace_dir = os.path.join(project_root, "traces")
    os.makedirs(trace_dir, exist_ok=True)
    tid = trace["trace_id"]
    tpath = os.path.join(trace_dir, tid + ".json")
    with open(tpath, "w", encoding="utf-8") as f:
        json.dump(trace, f, ensure_ascii=False, indent=2)


def _print_trace_summary(trace: dict) -> None:
    """Print human-readable trace summary."""
    status = "SUCCESS" if trace["success"] else "FAILED"
    tid = trace["trace_id"]
    print(f"\n[Trace {tid}] {status}")
    for s in trace.get("steps", []):
        icon = "->" if s["action"] == "final_answer" else ">>"
        act = s["action"] or "?"
        ai = json.dumps(s.get("action_input"), ensure_ascii=False) if s.get("action_input") else "null"
        obs = (s.get("observation") or "")[:80].replace("\n", " ")
        sid = s["step"]; lm = s["latency_ms"]
        if obs:
            print(f"  {icon} Step {sid}: {act}({ai[:40]}) [{lm}ms] => {obs}...")
        else:
            fa = s.get("final_answer") or ""
            print(f"  {icon} Step {sid}: {act} => {fa[:80]}")
    total = trace.get("total_latency_ms", 0)
    nsteps = len(trace.get("steps", []))
    print(f"  Total: {nsteps} steps, {total}ms")


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
        def _confirm(name: str, args_preview: str) -> bool:
            print(f"\n\n \u26a0\ufe0f  Dangerous tool: {name}")
            print(f"   Args: {args_preview}")
            ans = input("   Confirm? (y/n): ").strip().lower()
            return ans == "y"
        new_history, result, trace = react_agent(user_input, verbose=True, conversation_history=conversation_history, confirm_cb=_confirm)
        if new_history is not None and result is not None:
            conversation_history.append({"role": "user", "content": user_input})
            conversation_history.extend(new_history)
            # new_history already ends with the assistant final_answer message
        if trace:
            _print_trace_summary(trace)
        print("\nFinal Answer: " + result)
        conversation_history = truncate_messages([{"role": "system", "content": ""}] + conversation_history, max_tokens=2000)[1:]
    print("\nGoodbye!")