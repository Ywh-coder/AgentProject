import requests
from bs4 import BeautifulSoup
from urllib.parse import quote
import logging

logger = logging.getLogger(\"ReActAgent.tools\")


def calculator(expression: str) -> str:
    \"\"\"安全计算数学表达式\"\"\"
    from simpleeval import simple_eval
    try:
        result = simple_eval(expression, names={})
        return str(result)
    except Exception as e:
        return f\"计算错误: {e}\"


def web_search(query: str) -> str:
    \"\"\"增强版搜索：优先使用 Bing（国内可访问），失败则回退 DuckDuckGo\"\"\"
    # 1. 尝试 Bing 搜索
    try:
        headers = {
            \"User-Agent\": \"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0\"
        }
        url = f\"https://www.bing.com/search?q={quote(query)}\"
        resp = requests.get(url, headers=headers, timeout=8)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        results = []
        for li in soup.select('ol#b_results li.b_algo')[:3]:
            h2 = li.find('h2')
            if not h2:
                continue
            a = h2.find('a')
            if not a:
                continue
            title = a.get_text(strip=True)
            caption = li.find('div', class_='b_caption')
            if caption:
                p = caption.find('p')
                body = p.get_text(strip=True) if p else caption.get_text(strip=True)
            else:
                body = \"\"
            if title and body:
                if len(body) > 200:
                    body = body[:200] + '...'
                results.append(f\"{title}: {body}\")
        if results:
            return chr(10).join(results)
        else:
            # 备用选择器
            for li in soup.find_all('li', class_='b_algo')[:3]:
                h2 = li.find('h2')
                if not h2:
                    continue
                a = h2.find('a')
                if not a:
                    continue
                title = a.get_text(strip=True)
                body = li.get_text(strip=True)
                if title in body:
                    body = body.replace(title, '').strip()
                if body:
                    if len(body) > 200:
                        body = body[:200] + '...'
                    results.append(f\"{title}: {body}\")
            if results:
                return chr(10).join(results)
            return \"未在 Bing 找到相关结果，请尝试其他关键词。\"
    except Exception as e:
        logger.warning(f\"Bing 搜索失败: {e}，尝试 DuckDuckGo\")

    # 2. 回退 DuckDuckGo
    try:
        from ddgs import DDGS
        results = DDGS().text(query, max_results=3, timeout=15)
        if results:
            formatted = []
            for r in results:
                title = r.get('title', '无标题')
                body = r.get('body', '')
                if len(body) > 200:
                    body = body[:200] + '...'
                formatted.append(f\"{title}: {body}\")
            return chr(10).join(formatted)
        return \"DuckDuckGo 未返回结果。\"
    except Exception as e:
        logger.error(f\"DuckDuckGo 也失败: {e}\")
        return f\"搜索失败：所有引擎均超时或出错，请稍后再试。\"
