import sys
import json
import os
import re
import logging
from typing import Dict, Any, List, Optional, Callable
import locale
import time
import uuid
from openai import (
    APIError, APITimeoutError, RateLimitError,
    AuthenticationError, BadRequestError, APIConnectionError,
    OpenAI
)
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


_client: Optional[OpenAI] = None

def get_client() -> OpenAI:
    """延迟创建 OpenAI 客户端，避免导入时因缺少 API Key 崩溃。"""
    global _client
    if _client is None:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("环境变量 DEEPSEEK_API_KEY 未设置，请在 .env 文件中配置。")
        _client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com/v1",
        )
    return _client
from .tools import calculator, web_search, send_email, TOOLS_SCHEMA



DEFAULT_MAX_FAILURES = 3
# ---------- Circuit Breaker ----------
class CircuitBreaker:
    """连续失败达到阈值后熔断，冷却期结束后自动半开重试。"""

    def __init__(self, max_failures: int = DEFAULT_MAX_FAILURES, cooldown_s: float = 60.0):
        self._failures: Dict[str, int] = {}
        self._open_until: Dict[str, float] = {}
        self._max = max_failures
        self._cooldown = cooldown_s

    def is_open(self, name: str) -> bool:
        until = self._open_until.get(name, 0.0)
        if until and time.time() >= until:
            # 冷却结束，半开：清空计数，允许一次尝试
            self._open_until.pop(name, None)
            self._failures.pop(name, None)
            return False
        return until > time.time()

    def record(self, name: str) -> None:
        n = self._failures.get(name, 0) + 1
        self._failures[name] = n
        if n >= self._max:
            self._open_until[name] = time.time() + self._cooldown

    def reset(self, name: str) -> None:
        self._failures.pop(name, None)
        self._open_until.pop(name, None)

    def reset_all(self) -> None:
        self._failures.clear()
        self._open_until.clear()

    def _peek_open(self, name: str) -> bool:
        """只读查询，不修改状态。"""
        return self._open_until.get(name, 0.0) > time.time()

    def get_stats(self) -> Dict[str, Dict]:
        return {
            name: {
                "failure_count": self._failures.get(name, 0),
                "circuit_open": self._peek_open(name),
            }
            for name in set(self._failures) | set(self._open_until)
        }

circuit_breaker = CircuitBreaker()

# ---------- Tool Registry ----------
class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Dict] = {}
        self._stats: Dict[str, Dict[str, int]] = {"calls": {}, "success": {}, "failure": {}}
    def register(self, name: str, description: str, parameters: Dict[str, Any], requires_confirmation: bool = False):
        def decorator(func: Callable):
            self._tools[name] = {
                "name": name,
                "description": description,
                "parameters": parameters,
                "function": func,
                "requires_confirmation": requires_confirmation,
                "strict": True,
                "schema": {  # 保留原始 schema（含 strict/examples 等）
                    "name": name,
                    "description": description,
                    "strict": True,
                    "parameters": parameters,
                },
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
        if tool.get("requires_confirmation"):
            if confirm_cb is None:
                return f"Tool {name} requires confirmation but no confirm_cb provided. Aborting."
            preview = json.dumps(args, ensure_ascii=False)
            if not confirm_cb(name, preview):
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
        """从已注册的工具生成 schema，保证与注册表一致。"""
        return [t["schema"] for t in self._tools.values()]
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

def _register_from_schema(func, requires_confirmation: bool = False) -> None:
    """按函数名在 TOOLS_SCHEMA 中查找对应 schema 并注册。"""
    name = func.__name__
    schema = next((t for t in TOOLS_SCHEMA if t["name"] == name), None)
    if schema is None:
        raise ValueError(f"TOOLS_SCHEMA 中缺少 '{name}' 的定义")
    registry.register(
        name=name,
        description=schema["description"],
        parameters=schema["parameters"],
        requires_confirmation=requires_confirmation,
    )(func)

_register_from_schema(calculator, requires_confirmation=False)
_register_from_schema(web_search, requires_confirmation=False)
_register_from_schema(send_email, requires_confirmation=True)
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
    try:
        ENCODING = tiktoken.encoding_for_model("deepseek-chat")
    except KeyError:
        ENCODING = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(ENCODING.encode(text))
except ImportError:
    logger.warning("tiktoken 未安装，使用字符数估算 token（1 token≈4 字符）")
    def count_tokens(text: str) -> int:
        return len(text) // 4



def _is_observation(msg: Dict[str, str]) -> bool:
    """Check if a user message is a tool observation (vs. a real user query)."""
    if msg.get("role") != "user":
        return False
    content = msg.get("content", "")
    return content.startswith("[Reasoning:") or content.startswith("Observation:")


def _group_turns(messages: List[Dict[str, str]]) -> tuple:
    """Group messages into turns after the system prompt.

    A turn is: [user_query, assistant_response, observation]  (may have multiple assistant-observation pairs)
    Returns (turns, orphaned_msgs) where each turn is a list of messages.
    """
    turns: List[List[Dict]] = []
    orphaned: List[Dict] = []
    i = 1  # skip system prompt at index 0

    while i < len(messages):
        msg = messages[i]
        # Real user query: role=user and NOT an observation
        if msg.get("role") == "user" and not _is_observation(msg):
            # Start a new turn; collect following assistant+observation pairs
            turn = [msg]
            i += 1
            while i < len(messages):
                m = messages[i]
                if m.get("role") == "assistant":
                    turn.append(m)
                    i += 1
                    # Optional observation following this assistant message
                    if i < len(messages) and _is_observation(messages[i]):
                        turn.append(messages[i])
                        i += 1
                elif m.get("role") == "user" and not _is_observation(m):
                    # Next user query — current turn has no assistant response yet (orphaned at end)
                    break
                else:
                    # Unexpected message (e.g. observation without preceding assistant)
                    turn.append(m)
                    i += 1
            turns.append(turn)
        elif msg.get("role") == "user" and _is_observation(msg):
            # Observation without preceding assistant — append to last turn if possible
            if turns:
                turns[-1].append(msg)
            else:
                orphaned.append(msg)
            i += 1
        else:
            # Other unexpected message — treat as orphan
            orphaned.append(msg)
            i += 1

    return turns, orphaned


def truncate_messages(messages: List[Dict[str, str]], max_tokens: int = 3000) -> List[Dict[str, str]]:
    """Trim old conversation turns (keeping system prompt) to fit within max_tokens.

    A 'turn' is a complete user-query → assistant-response (+ optional observation) block.
    Orphaned messages are preserved at the end.
    Truncation removes entire turns from the end; never splits a turn in the middle.
    """
    total = sum(count_tokens(m.get("content", "")) for m in messages)
    if total <= max_tokens:
        return messages

    turns, orphaned = _group_turns(messages)
    # Estimate token cost of orphaned messages
    orphan_tokens = sum(count_tokens(m.get("content", "")) for m in orphaned)
    # Keep at least 20% buffer in case estimates are off
    safe_limit = int(max_tokens * 0.8)

    # Pop complete turns from the end until the remaining content fits
    # (orphan tokens are always kept, so compare remaining against safe_limit)
    while turns:
        oldest = turns[0]
        oldest_tokens = sum(count_tokens(m.get("content", "")) for m in oldest)
        if total - oldest_tokens <= safe_limit:
            break
        turns.pop(0)
        total -= oldest_tokens
    # Rebuild: system prompt + remaining turns + any orphaned messages
    result = [messages[0]] if messages else []
    for turn in turns:
        result.extend(turn)
    result.extend(orphaned)
    return result
# ---------- LLM 调用 ----------
# ---------- LLM Call with timeout and retry ----------
def call_llm(messages: List[Dict[str, str]], temperature: float = 0.0, max_retries: int = 3) -> Optional[str]:
    NON_RETRYABLE = (AuthenticationError, BadRequestError)
    last_err: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            response = get_client().chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                temperature=temperature,
                timeout=30.0,
            )
            return response.choices[0].message.content
        except NON_RETRYABLE as e:
            logger.error(f"LLM non-retryable error: {e}")
            return None
        except RateLimitError as e:
            wait = (2 ** attempt) * 2
            last_err = e
        except (APITimeoutError, APIConnectionError, APIError) as e:
            wait = 2 ** attempt
            last_err = e
        except Exception as e:
            logger.error(f"LLM unexpected error: {e}")
            return None

        if attempt < max_retries - 1:
            logger.warning(
                f"LLM call failed (attempt {attempt+1}/{max_retries}): "
                f"{last_err}, retry in {wait}s"
            )
            time.sleep(wait)
        else:
            logger.error(f"LLM call failed after {max_retries} attempts: {last_err}")
    return None
# ---------- 解析模型输出 ----------
def parse_action(text: str) -> tuple:
    """从模型输出中提取 (action, action_input, thought)。"""
    text = re.sub(r'`json\s*|`\s*', '', text).strip()
    start = text.find('{')
    if start == -1:
        raise ValueError("未找到 JSON 起始符 '{'")

    brace_count, end = 0, start
    for i, ch in enumerate(text[start:], start):
        if ch == '{':   brace_count += 1
        elif ch == '}':
            brace_count -= 1
            if brace_count == 0:
                end = i
                break
    if end == start:
        raise ValueError("未找到匹配的 JSON 结束符 '}'")

    try:
        data = json.loads(text[start:end+1])
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}")

    thought = data.get("thought", "")
    if "action" in data and data["action"] is not None:
        return data["action"], data.get("action_input"), thought
    if "final_answer" in data:
        return "final", data["final_answer"], thought
    raise ValueError("JSON missing action or final_answer field")
# ---------- ReAct Loop with multi-turn support ----------
def react_agent(user_query: str, max_steps: int = 5, max_parse_retries: int = 3, verbose: bool = True, conversation_history: Optional[List[Dict]] = None, confirm_cb=None, save_trace: bool = False) -> tuple:
    """Returns (new_history, final_answer, trace).
    new_history: list of assistant+observation messages added this turn.
    final_answer: str or None.
    trace: dict with full execution trace.
    """

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
            if save_trace: _save_trace(trace)
            return [], None, trace
        if verbose:
            logger.info(f"Model response: {response}")
        # Extract thought from response
        thought = ""  # 由 parse_action 填充
        parse_ok = False
        parse_messages = list(messages)
        for retry in range(max_parse_retries):
            try:
                action, action_input, thought = parse_action(response)
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
                    if save_trace: _save_trace(trace)
                    return [], None, trace
                continue
        if not parse_ok:
            trace["error"] = "Parse retry exhausted"
            trace["total_latency_ms"] = int((time.time() - step_start_global) * 1000)
            if save_trace: _save_trace(trace)
            return [], None, trace
        if verbose:
            logger.info(f"Action: {action}, Input: {action_input}, Thought: {thought}")
        if action == "final_answer" or action == "final":
            final_answer = action_input if isinstance(action_input, str) else json.dumps(action_input, ensure_ascii=False)
            messages.append({"role": "assistant", "content": response})
            # Extract only the assistant+observation pairs added this turn.
            # messages = [system, ...prev_history, user_query, assistant1, obs1, ...]
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
            if save_trace: _save_trace(trace)
            return new_history, final_answer, trace
        if not isinstance(action_input, dict):
            tool_info = next((t for t in registry.get_all() if t["name"] == action), None)
            if tool_info:
                param_names = list(tool_info["parameters"].get("properties", {}).keys())
                if len(param_names) == 1:
                    action_input = {param_names[0]: action_input}
                else:
                    observation = f"Error: tool {action} requires parameters but received a non-dict value. Please provide correct parameters."
                    messages.append({"role": "assistant", "content": response})
                    messages.append({"role": "user", "content": observation})
                    trace["steps"].append({
                        "step": step + 1, "thought": thought, "action": action,
                        "action_input": action_input, "observation": observation,
                        "latency_ms": int((time.time() - step_start) * 1000),
                    })
                    continue
            else:
                tool_names = ", ".join(t["name"] for t in registry.get_all())
                observation = f"Error: unknown tool \"{action}\". Available tools: {tool_names}. Please choose a valid tool."
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": observation})
                trace["steps"].append({
                    "step": step + 1, "thought": thought, "action": action,
                    "action_input": action_input, "observation": observation,
                    "latency_ms": int((time.time() - step_start) * 1000),
                })
                continue
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
    trace["error"] = f"Max steps ({max_steps}) reached without a final answer"
    if save_trace: _save_trace(trace)
    return [], f"(Max steps reached: {max_steps} steps used without reaching a conclusion.)", trace


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

    def _confirm(name: str, args_preview: str) -> bool:
        print(f"\n\n \u26a0\ufe0f  Dangerous tool: {name}")
        print(f"   Args: {args_preview}")
        ans = input("   Confirm? (y/n): ").strip().lower()
        return ans == "y"
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

        new_history, result, trace = react_agent(user_input, verbose=True, conversation_history=conversation_history, confirm_cb=_confirm, save_trace=True)
        if new_history is not None and result is not None:
            conversation_history.append({"role": "user", "content": user_input})
            conversation_history.extend(new_history)
            # new_history already ends with the assistant final_answer message
        if trace:
            _print_trace_summary(trace)
        print("\nFinal Answer: " + (result or "No final answer. See trace for details."))
        conversation_history = truncate_messages([{"role": "system", "content": ""}] + conversation_history, max_tokens=2000)[1:]
    print("\nGoodbye!")