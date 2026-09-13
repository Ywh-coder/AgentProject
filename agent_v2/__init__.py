from .agent import react_agent, ToolRegistry, CircuitBreaker, registry, main, DEFAULT_MAX_FAILURES
from .tools import calculator, web_search, send_email, TOOLS_SCHEMA
from eval.run_eval import run_eval

__version__ = "2.0.0"
__all__ = [
    "react_agent", "ToolRegistry", "CircuitBreaker", "registry", "main", "DEFAULT_MAX_FAILURES", 
    "TOOLS_SCHEMA", "calculator", "web_search", "send_email",
    "run_eval",
]
