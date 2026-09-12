from .agent import react_agent, ToolRegistry, CircuitBreaker, registry, main
from .tools import calculator, web_search, TOOLS_SCHEMA

__version__ = "2.0.0"
__all__ = ["send_email", "react_agent", "ToolRegistry", "CircuitBreaker", "registry", "main", "TOOLS_SCHEMA", "calculator", "web_search"]
from .eval import EvalRunner, EvalQuestion, EVALUATION_SUITE, run_eval
__all__ = ["send_email", "react_agent", "ToolRegistry", "CircuitBreaker", "registry", "main", "TOOLS_SCHEMA", "calculator", "web_search", "EvalRunner", "EvalQuestion", "EVALUATION_SUITE", "run_eval"]
