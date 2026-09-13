"""Evaluation module for ReAct Agent.

Usage:
    from agent_v2.eval import run_eval, EVALUATION_SUITE
    results = run_eval(max_steps=5)
    print(results.summary())
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .agent import react_agent

logger = logging.getLogger("ReActAgent.eval")


@dataclass
class EvalQuestion:
    """A single evaluation question."""
    question: str
    expected_answer: str
    keywords: List[str] = field(default_factory=list)
    category: str = "general"


# ---------- Evaluation Suite ----------
EVALUATION_SUITE: List[EvalQuestion] = [
    # --- Calculator questions ---
    EvalQuestion(
        question="What is 17 * 23?",
        expected_answer="391",
        keywords=["391"],
        category="calc",
    ),
    EvalQuestion(
        question="Calculate the square root of 144 plus 10.",
        expected_answer="22",
        keywords=["22"],
        category="calc",
    ),
    EvalQuestion(
        question="What is (100 + 50) / 3?",
        expected_answer="50",
        keywords=["50"],
        category="calc",
    ),
    # --- Knowledge questions (need web search) ---
    EvalQuestion(
        question="What is the capital of France?",
        expected_answer="Paris",
        keywords=["paris"],
        category="knowledge",
    ),
    EvalQuestion(
        question="Who wrote Romeo and Juliet?",
        expected_answer="Shakespeare",
        keywords=["shakespeare"],
        category="knowledge",
    ),
    # --- Multi-step ---
    EvalQuestion(
        question="What is 5 factorial? (5! = 5*4*3*2*1)",
        expected_answer="120",
        keywords=["120"],
        category="calc",
    ),
]


@dataclass
class EvalResult:
    question: str
    passed: bool
    expected: str
    actual: str
    steps_used: int
    category: str


class EvalRunner:
    def __init__(self, questions: Optional[List[EvalQuestion]] = None):
        self.questions = questions or EVALUATION_SUITE
        self.results: List[EvalResult] = []

    def _check_answer(self, question: EvalQuestion, answer: str) -> bool:
        """Check if the answer matches expected (fuzzy match via keywords)."""
        answer_lower = answer.lower()
        # Exact match first
        if question.expected_answer.lower() in answer_lower:
            return True
        # Keyword match
        if question.keywords:
            return any(kw.lower() in answer_lower for kw in question.keywords)
        return False

    def run(self, max_steps: int = 5, verbose: bool = True) -> List[EvalResult]:
        self.results = []
        for i, q in enumerate(self.questions):
            if verbose:
                print(f"\n[{i+1}/{len(self.questions)}] ({q.category}) {q.question}")
            history, answer, trace = react_agent(q.question, max_steps=max_steps, verbose=False)
            passed = self._check_answer(q, answer)
            result = EvalResult(
                question=q.question,
                passed=passed,
                expected=q.expected_answer,
                actual=answer,
                steps_used=len(trace.get("steps", [])) if trace else 0,
                category=q.category,
            )
            self.results.append(result)
            status = "PASS" if passed else "FAIL"
            print(f"  [{status}] Expected: {q.expected_answer} | Got: {answer}")
        return self.results

    def summary(self) -> str:
        if not self.results:
            return "No results. Run eval first."
        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed)
        by_cat: Dict[str, Dict[str, int]] = {}
        for r in self.results:
            by_cat.setdefault(r.category, {"pass": 0, "total": 0})
            by_cat[r.category]["total"] += 1
            if r.passed:
                by_cat[r.category]["pass"] += 1
        lines = [
            f"=== Eval Summary: {passed}/{total} passed ({100*passed//total}%) ===",
        ]
        for cat, counts in sorted(by_cat.items()):
            lines.append(f"  [{cat}] {counts['pass']}/{counts['total']}")
        lines.append("")
        for r in self.results:
            icon = "✓" if r.passed else "✗"
            lines.append(f"  {icon} {r.question[:50]}...")
            if not r.passed:
                lines.append(f"    Expected: {r.expected} | Got: {r.actual[:80]}")
        return "\n".join(lines)


def run_eval(max_steps: int = 5, verbose: bool = True) -> List[EvalResult]:
    """Convenience function to run the full evaluation suite."""
    runner = EvalRunner()
    runner.run(max_steps=max_steps, verbose=verbose)
    print(runner.summary())
    return runner.results
