#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evaluation runner for ReAct Agent.

Usage:
    python -m eval.run_eval
    python -m eval.run_eval --test-file eval/test_cases.jsonl
"""

import json
import time
import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_v2.agent import react_agent


def extract_called_tools(history):
    """Extract tool names called from conversation history."""
    called = []
    for msg in history:
        if msg["role"] != "assistant":
            continue
        content = msg.get("content", "")
        # Try to parse JSON from the response
        start = content.find("{")
        if start == -1:
            continue
        end = content.rfind("}")
        if end <= start:
            continue
        try:
            data = json.loads(content[start:end+1])
            action = data.get("action", "")
            if action and action != "final":
                called.append(action)
        except (json.JSONDecodeError, ValueError):
            pass
    return called


def run_eval(test_file: str = "eval/test_cases.jsonl", max_steps: int = 5, output_dir: str = "eval") -> list:
    """Run evaluation suite and return results."""
    # Load test cases
    with open(test_file, "r", encoding="utf-8-sig") as f:
        cases = [json.loads(line) for line in f if line.strip()]

    print(f"Loaded {len(cases)} test cases from {test_file}")
    results = []

    for i, case in enumerate(cases, 1):
        print(f"\n[{i}/{len(cases)}] ({case['category']}) {case['query'][:50]}...")
        start = time.time()
        history, answer, trace = react_agent(case["query"], max_steps=max_steps, verbose=False)
        elapsed = time.time() - start

        called_tools = extract_called_tools(history)
        expected_tools = case.get("expected_tools", [])
        expected_kw = case.get("expected_keywords", [])

        tool_match = set(called_tools) == set(expected_tools)
        answer_ok = all(kw.lower() in answer.lower() for kw in expected_kw) if expected_kw else True

        result = {
            "id": case["id"],
            "category": case["category"],
            "query": case["query"],
            "answer": answer,
            "expected_tools": expected_tools,
            "called_tools": called_tools,
            "tool_match": tool_match,
            "answer_ok": answer_ok,
            "latency_s": round(elapsed, 2),
            "steps": len(called_tools),
        }
        results.append(result)
        status = "PASS" if tool_match and answer_ok else "FAIL"
        print(f"  [{status}] tools={called_tools} answer={answer[:80]}")

    # Summary
    total = len(results)
    tool_correct = sum(1 for r in results if r["tool_match"])
    answer_correct = sum(1 for r in results if r["answer_ok"])
    avg_latency = sum(r["latency_s"] for r in results) / total
    avg_steps = sum(r["steps"] for r in results) / total

    print("\n" + "=" * 50)
    print(f"总用例: {total}")
    print(f"工具调用准确率: {tool_correct}/{total} = {tool_correct/total:.1%}")
    print(f"答案正确率:     {answer_correct}/{total} = {answer_correct/total:.1%}")
    print(f"平均延迟:       {avg_latency:.2f}s")
    print(f"平均步数:       {avg_steps:.2f}")

    # By category
    by_cat = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)

    print("\n按类别:")
    for cat in sorted(by_cat.keys()):
        rs = by_cat[cat]
        tc = sum(1 for r in rs if r["tool_match"])
        ac = sum(1 for r in rs if r["answer_ok"])
        print(f"  {cat}: 工具={tc}/{len(rs)} ({tc/len(rs):.0%})  答案={ac}/{len(rs)} ({ac/len(rs):.0%})")

    # Save JSON report
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "eval_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n详细结果已保存到: {report_path}")

    # Generate markdown report
    md_lines = [
        "# 评估报告",
        "",
        "## 总览",
        f"- 测试集: {total} 条",
        f"- 工具调用准确率: {tool_correct}/{total} = {tool_correct/total:.1%}",
        f"- 答案正确率: {answer_correct}/{total} = {answer_correct/total:.1%}",
        f"- 平均步数: {avg_steps:.2f}",
        f"- 平均延迟: {avg_latency:.2f}s",
        "",
        "## 按类别",
        "",
        "| 类别 | 数量 | 工具准确率 | 答案正确率 |",
        "|---|---|---|---|",
    ]
    for cat in sorted(by_cat.keys()):
        rs = by_cat[cat]
        tc = sum(1 for r in rs if r["tool_match"])
        ac = sum(1 for r in rs if r["answer_ok"])
        md_lines.append(f"| {cat} | {len(rs)} | {tc}/{len(rs)} ({tc/len(rs):.0%}) | {ac}/{len(rs)} ({ac/len(rs):.0%}) |")

    # Failed cases
    failures = [r for r in results if not (r["tool_match"] and r["answer_ok"])]
    if failures:
        md_lines += ["", "## 失败案例", ""]
        for r in failures:
            reasons = []
            if not r["tool_match"]:
                reasons.append(f"期望工具 {r['expected_tools']}，实际调用 {r['called_tools']}")
            if not r["answer_ok"]:
                reasons.append(f"答案未包含预期关键词")
            md_lines.append(f"- **{r['id']}**: {'; '.join(reasons)} — 回答: {r['answer'][:60]}")

    md_path = os.path.join(output_dir, "eval_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines) + "\n")
    print(f"Markdown 报告已保存到: {md_path}")

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run ReAct Agent evaluation")
    parser.add_argument("--test-file", default="eval/test_cases.jsonl")
    parser.add_argument("--max-steps", type=int, default=5)
    args = parser.parse_args()
    run_eval(args.test_file, args.max_steps)
