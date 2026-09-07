import openai, json
def call_llm(prompt):
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    return response.choices[0].message.content

def calculator(expression: str) -> str:
    """安全地计算数学表达式，当需进行数据计算时调用"""
    try:
        # 注意：实际应使用 eval 的安全替代，如 ast 或 simpleeval
        result = eval(expression)
        return str(result)
    except Exception as e:
        return f"错误: {e}"

def parse_action(llm_text):
    # 找到第一个 '{' 和最后一个 '}' 之间的内容
    start = llm_text.find('{')
    end = llm_text.rfind('}')
    json_str = llm_text[start:end+1]
    data = json.loads(json_str)
    return data["action"], data["action_input"]

def build_prompt(user_query, tools_desc):
    tools_text = "\n".join([f"- {t['name']}: {t['description']}" for t in TOOLS])
    prompt = f"""你是一个智能助手，可以使用以下工具：

{tools_text}

用户问题：{user_query}

请选择要使用的工具，或直接回答。输出格式：
如果使用工具，输出 JSON：{{"action": "工具名", "action_input": {{...}}}}
如果直接回答，输出 JSON：{{"action": "final", "action_input": "最终答案"}}
"""
    return prompt
TOOLS = [
    {
        "name": "calculator",
        "description": "计算数学表达式，输入如 '2+3*4'",
        "parameters": {"expression": "str"},
        "function": calculator,
    },
    # 可以扩展其他工具
]
def execute_tool(action, action_input):
    for tool in TOOLS:
        if tool["name"] == action:
            return tool["function"](**action_input)
    return "工具不存在"