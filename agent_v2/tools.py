import requests
from bs4 import BeautifulSoup
from urllib.parse import quote
import logging
from typing import Dict, Any, List

logger = logging.getLogger('ReActAgent.tools')



TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        'name': 'calculator',
        'description': (
            'safely evaluate a mathematical expression. '
            'Supports arithmetic (+, -, *, /, //, %) and functions (sqrt, sin, cos, tan, log). '
            'Use ONLY for numeric computation. '
            'DO NOT use for unit conversion, currency exchange, or fact-finding.'
        ),
        'strict': True,
        'parameters': {
            'type': 'object',
            'properties': {
                'expression': {
                    'type': 'string',
                    'description': 'the math expression to evaluate, e.g. 2+3*4 or sqrt(16)+1',
                    'examples': ['2+3*4', 'sqrt(16)+1', '(10+5)/3'],
                }
            },
            'required': ['expression'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'web_search',
        'description': (
            'search the internet for real-time information via Bing or DuckDuckGo. '
            'Use for factual queries, news, weather, current events. '
            'DO NOT use for calculations or simple trivia.'
        ),
        'strict': True,
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'description': 'brief search query, 2-6 keywords',
                    'minLength': 1,
                    'maxLength': 100,
                    'examples': ['Beijing weather today', 'Python asyncio tutorial', 'OpenAI GPT-5 announcement'],
                }
            },
            'required': ['query'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'send_email',
        'description': (
            'send an email to a recipient. DANGEROUS operation — '
            'requires user confirmation before execution. '
            'Use ONLY when the user explicitly asks to send an email.'
        ),
        'strict': True,
        'parameters': {
            'type': 'object',
            'properties': {
                'to': {
                    'type': 'string',
                    'description': 'recipient email address',
                    'examples': ['user@example.com'],
                },
                'subject': {
                    'type': 'string',
                    'description': 'email subject line',
                    'examples': ['Meeting tomorrow', 'Hello'],
                },
                'body': {
                    'type': 'string',
                    'description': 'email body text',
                    'examples': ['Hi, are we still meeting at 3pm?'],
                },
            },
            'required': ['to', 'subject', 'body'],
            'additionalProperties': False,
        },
    },
]


def calculator(expression: str) -> str:
    from simpleeval import simple_eval
    try:
        result = simple_eval(expression, names={})
        return str(result)
    except Exception as e:
        return f'calculation error: {e}'


def web_search(query: str) -> str:
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0'
        }
        url = f'https://www.bing.com/search?q={quote(query)}'
        resp = requests.get(url, headers=headers, timeout=8)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        results = []
        for li in soup.select('ol#b_results li.b_algo')[:3]:
            h2 = li.find('h2')
            if not h2: continue
            a = h2.find('a')
            if not a: continue
            title = a.get_text(strip=True)
            caption = li.find('div', class_='b_caption')
            if caption:
                p = caption.find('p')
                body = p.get_text(strip=True) if p else caption.get_text(strip=True)
            else:
                body = ''
            if title and body:
                if len(body) > 200: body = body[:200] + '...'
                results.append(f'{title}: {body}')
        if results: return "\n".join(results)
        else:
            for li in soup.find_all('li', class_='b_algo')[:3]:
                h2 = li.find('h2')
                if not h2: continue
                a = h2.find('a')
                if not a: continue
                title = a.get_text(strip=True)
                body = li.get_text(strip=True)
                if title in body: body = body.replace(title, '').strip()
                if body:
                    if len(body) > 200: body = body[:200] + '...'
                    results.append(f'{title}: {body}')
            if results: return "\n".join(results)
            return 'No relevant results found on Bing. Try different keywords.'
    except Exception as e:
        logger.warning(f'Bing search failed: {e}, trying DuckDuckGo')
    try:
        from ddgs import DDGS
        results = DDGS().text(query, max_results=3, timeout=15)
        if results:
            formatted = []
            for r in results:
                title = r.get('title', 'No title')
                body = r.get('body', '')
                if len(body) > 200: body = body[:200] + '...'
                formatted.append(f'{title}: {body}')
            return "\n".join(formatted)
        return 'DuckDuckGo returned no results.'
    except Exception as e:
        logger.error(f'DuckDuckGo also failed: {e}')
        return f'Search failed: all engines timed out or errored. Please try again later.'


def send_email(to: str, subject: str, body: str) -> str:
    """Simulate sending an email. In production this would integrate with an SMTP server."""
    return f"[Simulated] Email sent to {to}, subject: {subject}"
