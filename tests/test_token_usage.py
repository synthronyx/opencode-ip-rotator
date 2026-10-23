import json
import unittest

import token_usage


class TokenEstimationTests(unittest.TestCase):
    def test_prompt_estimate_counts_whole_context(self):
        small = token_usage.estimate_prompt_tokens(
            {"messages": [{"role": "user", "content": "hi"}]}
        )
        big = token_usage.estimate_prompt_tokens(
            {
                "messages": [
                    {"role": "system", "content": "instructions " * 200},
                    {"role": "user", "content": "context " * 2000},
                ],
                "tools": [{"type": "function", "function": {"name": "read"}}],
            }
        )
        self.assertGreater(small, 0)
        self.assertGreater(big, small * 10)

    def test_prompt_estimate_includes_anthropic_system(self):
        without = token_usage.estimate_prompt_tokens(
            {"messages": [{"role": "user", "content": "x"}]}
        )
        with_system = token_usage.estimate_prompt_tokens(
            {
                "system": "very long system prompt " * 100,
                "messages": [{"role": "user", "content": "x"}],
            }
        )
        self.assertGreater(with_system, without)

    def test_prompt_estimate_includes_responses_instructions(self):
        payload = {
            "input": "short",
            "instructions": "follow these rules " * 100,
        }
        self.assertGreater(token_usage.estimate_prompt_tokens(payload), 100)

    def test_text_estimate(self):
        self.assertEqual(token_usage.estimate_text_tokens(""), 0)
        self.assertGreater(token_usage.estimate_text_tokens("hello world"), 0)
        self.assertGreaterEqual(token_usage.estimate_text_tokens("你好世界"), 4)

    def test_completion_estimate_uses_message_content_only(self):
        res_json = {
            "id": "chatcmpl-123",
            "usage": {},
            "choices": [
                {"message": {"role": "assistant", "content": "hello " * 100}}
            ],
        }
        self.assertGreater(token_usage.estimate_completion_tokens(res_json), 100)


class StreamObservationTests(unittest.TestCase):
    def test_openai_chunk_usage_and_text(self):
        stats = token_usage.new_stream_stats()
        token_usage.observe_stream_event(
            "data: "
            + json.dumps({"choices": [{"delta": {"content": "hello there"}}]}),
            stats,
        )
        token_usage.observe_stream_event(
            "data: "
            + json.dumps(
                {
                    "choices": [],
                    "usage": {"prompt_tokens": 1234, "completion_tokens": 9},
                }
            ),
            stats,
        )
        token_usage.observe_stream_event("data: [DONE]", stats)
        self.assertEqual(stats["usage"]["prompt_tokens"], 1234)
        self.assertEqual(stats["usage"]["completion_tokens"], 9)
        self.assertGreater(stats["ascii_chars"], 0)

    def test_openai_tool_arguments_counted(self):
        stats = token_usage.new_stream_stats()
        token_usage.observe_stream_event(
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "name": "read",
                                            "arguments": '{"path":"a.py"}',
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ),
            stats,
        )
        self.assertGreater(stats["ascii_chars"], 0)

    def test_anthropic_stream_merges_usage(self):
        stats = token_usage.new_stream_stats()
        token_usage.observe_stream_event(
            "data: "
            + json.dumps(
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 800, "output_tokens": 1}},
                }
            ),
            stats,
        )
        token_usage.observe_stream_event(
            "data: "
            + json.dumps(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "hello"},
                }
            ),
            stats,
        )
        token_usage.observe_stream_event(
            "data: " + json.dumps({"type": "message_delta", "usage": {"output_tokens": 42}}),
            stats,
        )
        self.assertEqual(stats["usage"]["input_tokens"], 800)
        self.assertEqual(stats["usage"]["output_tokens"], 42)
        self.assertGreater(stats["ascii_chars"], 0)

    def test_anthropic_zero_input_does_not_clear_known_input(self):
        stats = token_usage.new_stream_stats()
        token_usage.observe_stream_event(
            'data: {"type":"message_start","message":{"usage":{"input_tokens":500}}}',
            stats,
        )
        token_usage.observe_stream_event(
            'data: {"type":"message_delta","usage":{"input_tokens":0,"output_tokens":5}}',
            stats,
        )
        self.assertEqual(stats["usage"]["input_tokens"], 500)
        self.assertEqual(stats["usage"]["output_tokens"], 5)

    def test_responses_stream_completed_usage(self):
        stats = token_usage.new_stream_stats()
        token_usage.observe_stream_event(
            'data: {"type":"response.output_text.delta","delta":"hello"}', stats
        )
        token_usage.observe_stream_event(
            'data: {"type":"response.completed","response":{"usage":{"input_tokens":900,"output_tokens":12}}}',
            stats,
        )
        token_usage.observe_stream_event(
            'data: {"type":"response.function_call_arguments.delta","delta":"{\\"a\\":1}"}',
            stats,
        )
        self.assertEqual(stats["usage"]["input_tokens"], 900)
        self.assertEqual(stats["usage"]["output_tokens"], 12)
        self.assertGreater(stats["ascii_chars"], 0)

    def test_ignores_non_data_lines(self):
        stats = token_usage.new_stream_stats()
        token_usage.observe_stream_event("event: message_start", stats)
        token_usage.observe_stream_event(": keep-alive", stats)
        self.assertEqual(stats["usage"], {})


class FinalizeUsageTests(unittest.TestCase):
    def test_prefers_upstream_usage(self):
        stats = token_usage.new_stream_stats()
        stats["usage"] = {"prompt_tokens": 4321, "completion_tokens": 88}
        stats["ascii_chars"] = 10_000
        self.assertEqual(
            token_usage.finalize_usage(stats, prompt_tokens_estimate=7),
            (4321, 88, 0),
        )

    def test_prefers_anthropic_usage_fields(self):
        stats = token_usage.new_stream_stats()
        stats["usage"] = {"input_tokens": 2500, "output_tokens": 75}
        self.assertEqual(
            token_usage.finalize_usage(stats, prompt_tokens_estimate=10),
            (2500, 75, 0),
        )

    def test_falls_back_to_estimates(self):
        stats = token_usage.new_stream_stats()
        stats["ascii_chars"] = 4000
        prompt, completion, _ = token_usage.finalize_usage(stats, prompt_tokens_estimate=1234)
        self.assertEqual(prompt, 1234)
        self.assertGreater(completion, 0)

    def test_fallback_uses_streamed_text_for_completion(self):
        stats = token_usage.new_stream_stats()
        stats["ascii_chars"] = 800
        stats["non_ascii_chars"] = 20
        _, completion, _ = token_usage.finalize_usage(stats, prompt_tokens_estimate=100)
        self.assertEqual(completion, 220)

    def test_skips_empty_stream(self):
        stats = token_usage.new_stream_stats()
        self.assertIsNone(token_usage.finalize_usage(stats, prompt_tokens_estimate=0))


class UsageExtractionTests(unittest.TestCase):
    def test_openai_prompt_tokens_with_cached_details(self):
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 800},
        }
        self.assertEqual(
            token_usage.extract_usage_tokens(usage), (1000, 50, 800)
        )

    def test_anthropic_input_plus_cache_counters(self):
        usage = {
            "input_tokens": 200,
            "output_tokens": 30,
            "cache_read_input_tokens": 700,
            "cache_creation_input_tokens": 100,
        }
        self.assertEqual(
            token_usage.extract_usage_tokens(usage), (1000, 30, 700)
        )

    def test_deepseek_cache_hit_tokens(self):
        usage = {
            "prompt_tokens": 900,
            "completion_tokens": 40,
            "prompt_cache_hit_tokens": 600,
            "prompt_cache_miss_tokens": 300,
        }
        self.assertEqual(
            token_usage.extract_usage_tokens(usage), (900, 40, 600)
        )

    def test_responses_input_tokens_details(self):
        usage = {
            "input_tokens": 750,
            "output_tokens": 25,
            "input_tokens_details": {"cached_tokens": 500},
        }
        self.assertEqual(
            token_usage.extract_usage_tokens(usage), (750, 25, 500)
        )

    def test_finalize_reports_cached_tokens(self):
        stats = token_usage.new_stream_stats()
        stats["usage"] = {
            "prompt_tokens": 1000,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 900},
        }
        self.assertEqual(
            token_usage.finalize_usage(stats, prompt_tokens_estimate=123),
            (1000, 10, 900),
        )

    def test_cached_tokens_clamped_to_prompt(self):
        stats = token_usage.new_stream_stats()
        stats["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 5000},
        }
        self.assertEqual(
            token_usage.finalize_usage(stats, prompt_tokens_estimate=0),
            (100, 5, 100),
        )


class CostTests(unittest.TestCase):
    pricing = {"input_per_1m": 1.0, "output_per_1m": 2.0}

    def test_no_cache_full_price(self):
        self.assertAlmostEqual(
            token_usage.compute_cost(1_000_000, 1_000_000, 0, self.pricing),
            3.0,
        )

    def test_cache_read_gets_default_ten_percent_rate(self):
        # 1M prompt, all cached: $0.10; 1M output: $2.00.
        self.assertAlmostEqual(
            token_usage.compute_cost(1_000_000, 1_000_000, 1_000_000, self.pricing),
            2.10,
        )

    def test_partial_cache_split(self):
        # 500k full-price + 500k at 10% = 0.55; output 0.20.
        self.assertAlmostEqual(
            token_usage.compute_cost(1_000_000, 100_000, 500_000, self.pricing),
            0.75,
        )

    def test_explicit_cache_rate(self):
        pricing = {"input_per_1m": 1.0, "output_per_1m": 2.0, "cache_read_per_1m": 0.25}
        self.assertAlmostEqual(
            token_usage.compute_cost(1_000_000, 0, 1_000_000, pricing),
            0.25,
        )

    def test_cached_cannot_exceed_prompt(self):
        self.assertAlmostEqual(
            token_usage.compute_cost(100, 0, 5000, self.pricing),
            0.00001,
        )


if __name__ == "__main__":
    unittest.main()
