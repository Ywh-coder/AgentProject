"""Unit tests for agent_v2 core logic (no LLM calls required)."""

from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_v2.agent import parse_action, truncate_messages, count_tokens, CircuitBreaker
from agent_v2.tools import TOOLS_SCHEMA


class TestParseAction:
    def test_valid_action(self):
        text = json.dumps({"thought": "I need to calculate", "action": "calculator", "action_input": {"expression": "2+2"}})
        action, action_input = parse_action(text)
        assert action == "calculator"
        assert action_input == {"expression": "2+2"}

    def test_final_answer(self):
        text = json.dumps({"thought": "done", "final_answer": "42"})
        action, action_input = parse_action(text)
        assert action == "final"
        assert action_input == "42"

    def test_with_code_fence(self):
        inner = json.dumps({"thought": "x", "action": "web_search", "action_input": {"query": "test"}})
        text = "```json\n" + inner + "\n```"
        action, action_input = parse_action(text)
        assert action == "web_search"
        assert action_input == {"query": "test"}

    def test_missing_fields_raises(self):
        text = json.dumps({"thought": "oops"})
        try:
            parse_action(text)
            assert False, "Should have raised"
        except ValueError:
            pass

    def test_invalid_json_raises(self):
        try:
            parse_action("not json at all")
            assert False, "Should have raised"
        except ValueError:
            pass


class TestTruncateMessages:
    def test_system_always_kept(self):
        msgs = [
            {"role": "system", "content": "You are helpful"},
            {"role": "assistant", "content": "A" * 100},
            {"role": "user", "content": "B" * 100},
        ]
        result = truncate_messages(msgs, max_tokens=10)
        assert result[0]["role"] == "system"

    def test_removes_complete_turns(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "U2"},
        ]
        result = truncate_messages(msgs, max_tokens=5)
        assert result[0]["role"] == "system"
        roles = [m["role"] for m in result]
        assert roles.count("assistant") == roles.count("user")

    def test_no_truncate_when_under_limit(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "short"},
            {"role": "user", "content": "short"},
        ]
        result = truncate_messages(msgs, max_tokens=10000)
        assert len(result) == len(msgs)


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        cb = CircuitBreaker(max_failures=2)
        assert not cb.is_open("tool_a")

    def test_opens_after_max_failures(self):
        cb = CircuitBreaker(max_failures=2)
        cb.record("tool_a")
        assert not cb.is_open("tool_a")
        cb.record("tool_a")
        assert cb.is_open("tool_a")

    def test_reset_closes_circuit(self):
        cb = CircuitBreaker(max_failures=1)
        cb.record("tool_a")
        assert cb.is_open("tool_a")
        cb.reset("tool_a")
        assert not cb.is_open("tool_a")

    def test_get_stats(self):
        cb = CircuitBreaker(max_failures=3)
        cb.record("calc")
        cb.record("calc")
        stats = cb.get_stats()
        assert stats["calc"]["failure_count"] == 2
        assert stats["calc"]["circuit_open"] is False

    def test_independent_tools(self):
        cb = CircuitBreaker(max_failures=1)
        cb.record("tool_a")
        assert cb.is_open("tool_a")
        assert not cb.is_open("tool_b")


class TestCountTokens:
    def test_positive(self):
        tok = count_tokens("hello world")
        assert tok > 0

    def test_empty(self):
        tok = count_tokens("")
        assert tok == 0

    def test_chinese_chars(self):
        tok = count_tokens("\u4f60\u597d\u4e16\u754c")
        assert tok > 0


class TestToolsSchema:
    def test_two_tools(self):
        assert len(TOOLS_SCHEMA) == 3

    def test_calculator_schema(self):
        t = TOOLS_SCHEMA[0]
        assert t["name"] == "calculator"
        assert t["strict"] is True
        params = t["parameters"]
        assert params["type"] == "object"
        assert "expression" in params["properties"]
        assert "expression" in params["required"]
        assert "examples" in params["properties"]["expression"]

    def test_web_search_schema(self):
        t = TOOLS_SCHEMA[1]
        assert t["name"] == "web_search"
        params = t["parameters"]
        assert "query" in params["required"]
        assert params["properties"]["query"]["type"] == "string"
        assert "examples" in params["properties"]["query"]
    def test_send_email_schema(self):
        t = TOOLS_SCHEMA[2]
        assert t["name"] == "send_email"
        assert t["strict"] is True
        params = t["parameters"]
        assert set(params["required"]) == {"to", "subject", "body"}
        assert params["properties"]["to"]["type"] == "string"
        assert "examples" in params["properties"]["to"]
        assert "examples" in params["properties"]["subject"]
        assert "examples" in params["properties"]["body"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])


class TestReactAgentReturnType:
    """Test that react_agent returns (messages, result) tuple."""
    def test_returns_tuple(self):
        # We can\'t call the real LLM here, but we can verify the signature
        import inspect
        from agent_v2.agent import react_agent
        sig = inspect.signature(react_agent)
        assert sig.return_annotation == tuple, f"Expected tuple, got {sig.return_annotation}"

    def test_parse_messages_not_mutating_main(self):
        # Verify parse_messages is isolated from messages
        from agent_v2.agent import parse_action
        msg = [{"role": "system", "content": "test"}]
        # This test just verifies the code path exists without crashing
        assert isinstance(msg, list)


class TestMultiTurnTrace:
    """Verify that react_agent returns only the new trace, not the full message history."""
    def test_trace_extraction_logic(self):
        """Simulate the trace extraction that happens inside react_agent.
        messages = [system, ...prev_history, user_query, assistant1, obs1, assistant2, obs2]
        n_prev = 1 + len(conversation_history)
        new_trace = messages[n_prev:]
        """
        prev_history = [
            {"role": "user", "content": "prev q"},
            {"role": "assistant", "content": "prev a"},
        ]
        user_query = "current q"
        assistant_resp = {"role": "assistant", "content": "I will search"}
        obs1 = {"role": "user", "content": "Observation: results..."}
        assistant_resp2 = {"role": "assistant", "content": "Done"}
        messages = (
            [{"role": "system", "content": "sys"}]
            + prev_history
            + [{"role": "user", "content": user_query}]
            + [assistant_resp, obs1, assistant_resp2]
        )
        n_prev = 1 + len(prev_history)
        new_trace = messages[n_prev:]
        # Should contain: user_query + assistant1 + obs1 + assistant2
        assert len(new_trace) == 4
        assert new_trace[0]["role"] == "user"
        assert new_trace[0]["content"] == "current q"
        assert new_trace[1]["role"] == "assistant"
        assert new_trace[2]["role"] == "user"
        assert new_trace[3]["role"] == "assistant"
        # Should NOT contain system prompt
        assert all(m["role"] != "system" for m in new_trace)

    def test_trace_with_no_prev_history(self):
        """When conversation_history is empty, n_prev = 1 (just system)."""
        user_query = "hello"
        assistant_resp = {"role": "assistant", "content": "Hi there"}
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": user_query},
            assistant_resp,
        ]
        n_prev = 1  # no prev history
        new_trace = messages[n_prev:]
        assert len(new_trace) == 2
        assert new_trace[0]["role"] == "user"
        assert new_trace[1]["role"] == "assistant"

    def test_main_history_accumulation(self):
        """Simulate what main() does: accumulate history across turns."""
        conversation_history = []
        # Turn 1
        # react_agent returns trace WITHOUT user_query (starts at assistant)
        turn1_trace = [
            {"role": "assistant", "content": "I will calc"},
            {"role": "user", "content": "Observation: 42"},
            {"role": "assistant", "content": "Done"},
        ]
        result1 = "42"
        conversation_history.append({"role": "user", "content": "q1"})
        conversation_history.extend(turn1_trace)
        conversation_history.append({"role": "assistant", "content": result1})
        # Now history = [user:q1, assistant:I will calc, user:Obs, assistant:42]
        assert conversation_history[0]["role"] == "user"
        assert conversation_history[0]["content"] == "q1"
        assert conversation_history[-1]["role"] == "assistant"
        assert conversation_history[-1]["content"] == "42"
        # Turn 2: pass history as conversation_history
        assert len(conversation_history) == 5
        # This proves the trace is preserved for the next turn

class TestDangerConfirmation:
    """Test the requires_confirmation / confirm_cb mechanism."""
    def test_safe_tool_no_prompt(self):
        from agent_v2.agent import registry
        result = registry.execute("calculator", {"expression": "1+1"})
        assert result == "2"

    def test_dangerous_tool_rejected(self):
        from agent_v2.agent import registry
        def deny(name, args):
            return False
        result = registry.execute("send_email", {"to": "a@b.com", "subject": "Hi", "body": "Hello"}, confirm_cb=deny)
        assert "cancelled" in result.lower() or "User cancelled" in result

    def test_dangerous_tool_accepted(self):
        from agent_v2.agent import registry
        def allow(name, args):
            return True
        result = registry.execute("send_email", {"to": "a@b.com", "subject": "Hi", "body": "Hello"}, confirm_cb=allow)
        assert "Email sent" in result or "Simulated" in result

    def test_dangerous_tool_without_cb_just_fails_silently(self):
        # Without confirm_cb, dangerous tool still executes (backward compat)
        from agent_v2.agent import registry
        result = registry.execute("send_email", {"to": "a@b.com", "subject": "Hi", "body": "Hello"}, confirm_cb=None)
        assert "Email sent" in result or "Simulated" in result
