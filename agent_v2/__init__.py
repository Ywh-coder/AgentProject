from .agent import react_agent, ToolRegistry, CircuitBreaker, registry, main
from .tools import calculator, web_search, send_email, TOOLS_SCHEMA
from .eval import EvalRunner, EvalQuestion, EVALUATION_SUITE, run_eval

__version__ = "2.0.0"
__all__ = [
    "react_agent", "ToolRegistry", "CircuitBreaker", "registry", "main",
    "TOOLS_SCHEMA", "calculator", "web_search", "send_email",
    "EvalRunner", "EvalQuestion", "EVALUATION_SUITE", "run_eval",
]
