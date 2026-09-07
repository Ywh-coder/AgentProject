import sys
import locale
locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')
sys.stdout.reconfigure(encoding='utf-8')
import json
import os
from typing import List, Dict, Any
from simpleeval import simple_eval
from dotenv import load_dotenv
from openai import OpenAI
load_dotenv()   # 自动加载 .env 文件中的变量
# ==================== 工具定义 ====================
def calculator(expression: str) -> str:
    """
    安全地计算数学表达式（使用 simpleeval 等安全库）
    """
    try:
        result = simple_eval(expression)
        return str(result)
    except Exception as e:
        return f"计算错误: {e}"

def web_search(query: str) -> str:
    """
    使用 DDGS 进行网页搜索
    """
    try:
        from ddgs import DDGS
        results = DDGS().text(query, max_results=3)
        if not results:
            return "未找到相关结果。"
        return "\n".join([f"{r['title']}: {r['body']}" for r in results])
    except ImportError:
        return "错误：未安装 duckduckgo_search 库，请执行 pip install duckduckgo-search"
    except Exception as e:
        return f"搜索错误: {e}"

# 工具注册表
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "calculator",
        "description": "计算数学表达式，输入如 '2+3*4'，当需要进行数值计算时使用。",
        "parameters": {"expression": "str"},
        "function": calculator,
    },
    {
        "name": "web_search",
        "description": "搜索互联网获取最新信息，输入查询关键词，返回相关网页摘要。",
        "parameters": {"query": "str"},
        "function": web_search,
    },
    # 可以继续添加其他工具
]

# ==================== 提示词构建 ====================
def build_prompt(user_query: str, tools_desc: str) -> str:
    """
    构建系统提示词，要求模型输出 JSON 格式的动作。
    """
    prompt = f"""你是一个智能助手，可以使用以下工具：

{tools_desc}

用户问题：{user_query}

请选择要使用的工具，或直接回答。输出格式：
- 如果使用工具，输出 JSON：{{"action": "工具名", "action_input": {{"参数名": "参数值"}}}}
- 如果已经得到最终答案，输出 JSON：{{"action": "final", "action_input": "最终答案"}}

注意：只能输出一个 JSON 对象，不要输出其他内容。"""
    return prompt

def get_tools_description() -> str:
    """生成工具描述文本"""
    return "\n".join([f"- {t['name']}: {t['description']}" for t in TOOLS])

# ==================== LLM 调用 ====================


# 在全局或函数内初始化
client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),        # ① 改用新的环境变量名
    base_url="https://api.deepseek.com/v1",       # ② 指定 DeepSeek 的 endpoint
)

def call_llm(messages):
    response = client.chat.completions.create(
        model="deepseek-chat",                     # ③ 使用 DeepSeek 模型
        messages=messages,
        temperature=0,
    )
    return response.choices[0].message.content
# ==================== 解析模型输出 ====================
def parse_action(llm_text: str) -> tuple[str, Any]:
    """
    从模型输出中提取 action 和 action_input。
    模型可能输出多余文字，我们尝试提取第一个 '{' 和最后一个 '}' 之间的 JSON。
    """
    # 清理可能的代码块标记
    llm_text = llm_text.replace("```json", "").replace("```", "").strip()

    start = llm_text.find('{')
    end = llm_text.rfind('}')
    if start == -1 or end == -1 or start >= end:
        raise ValueError("输出中未找到有效的 JSON 对象")

    json_str = llm_text[start:end+1]
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}")

    action = data.get("action")
    action_input = data.get("action_input")
    if action is None:
        raise ValueError("JSON 中缺少 'action' 字段")

    return action, action_input

# ==================== 执行工具 ====================
def execute_tool(action: str, action_input: Any) -> str:
    """
    根据动作名称执行对应工具，返回观察结果。
    """
    for tool in TOOLS:
        if tool["name"] == action:
            # 确保 action_input 是字典
            if not isinstance(action_input, dict):
                # 如果是字符串，尝试包装成字典，假设参数名为第一个参数
                # 这里简化处理：将字符串作为唯一参数
                params = list(tool["parameters"].keys())
                if len(params) == 1:
                    action_input = {params[0]: action_input}
                else:
                    return f"工具 {action} 需要多个参数，但收到非字典输入: {action_input}"
            try:
                return tool["function"](**action_input)
            except TypeError as e:
                return f"工具调用参数错误: {e}"
            except Exception as e:
                return f"工具执行异常: {e}"
    return f"未知工具: {action}"

# ==================== 记忆截断 ====================
def truncate_messages(messages: List[Dict[str, str]], max_tokens: int = 3000) -> List[Dict[str, str]]:
    """
    简单截断消息列表，避免超过 token 限制。
    这里使用字符数粗略估算（1 token ≈ 4 字符）。
    """
    def estimate_tokens(msg_list):
        total_chars = sum(len(m["content"]) for m in msg_list)
        return total_chars // 4

    # 保留 system 消息（第一条）和最后一条 user 消息（通常是最新观察）
    # 从第二条开始删除，直到满足条件
    while estimate_tokens(messages) > max_tokens and len(messages) > 2:
        # 删除索引 1 的消息（跳过 system 和第一条 user，保留后续的 assistant 和 observation）
        # 但为了保持交替，我们删除最早的非 system 消息
        # 简化：直接 pop(1)
        messages.pop(1)
    return messages

# ==================== ReAct 主循环 ====================
def react_agent(user_query: str, max_steps: int = 5, verbose: bool = True) -> str:
    """
    ReAct 智能体主循环。
    """
    tools_desc = get_tools_description()
    system_prompt = build_prompt(user_query, tools_desc)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query},
    ]

    for step in range(max_steps):
        if verbose:
            print(f"\n--- 步骤 {step+1} ---")

        # 调用 LLM
        response = call_llm(messages)
        if not response:
            return "LLM 调用失败，任务中止。"

        if verbose:
            print(f"模型输出: {response}")

        # 解析动作
        try:
            action, action_input = parse_action(response)
        except Exception as e:
            # 解析失败，将错误反馈给模型，让它重新输出
            error_msg = f"你的输出格式有误：{e}，请重新按 JSON 格式输出。"
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": error_msg})
            if verbose:
                print(f"解析失败: {e}，要求模型重新输出")
            continue

        if verbose:
            print(f"动作: {action}, 输入: {action_input}")

        # 如果模型给出最终答案
        if action == "final":
            final_answer = action_input if isinstance(action_input, str) else json.dumps(action_input, ensure_ascii=False)
            return final_answer

        # 执行工具
        observation = execute_tool(action, action_input)
        if verbose:
            print(f"观察: {observation}")

        # 将模型输出和观察结果添加到对话历史
        messages.append({"role": "assistant", "content": response})
        messages.append({"role": "user", "content": f"Observation: {observation}"})

        # 截断消息避免过长
        messages = truncate_messages(messages)

    return "达到最大步数，任务未完成。"

# ==================== 主入口 ====================
def main():
    print("=== ReAct Agent 演示 ===")
    print("支持的工具：")
    for tool in TOOLS:
        print(f"  - {tool['name']}: {tool['description']}")

    while True:
        user_input = input("\n请输入问题（输入 exit 退出）: ").strip()
        if user_input.lower() == "exit":
            break
        if not user_input:
            continue

        result = react_agent(user_input, verbose=True)
        print(f"\n最终答案: {result}")

if __name__ == "__main__":
    main()