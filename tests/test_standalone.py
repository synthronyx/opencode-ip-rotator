import time
import unittest

import standalone


class SessionIdTests(unittest.TestCase):
    def test_minted_id_has_opencode_shape(self):
        session_id = standalone.mint_session_id()
        self.assertTrue(standalone.is_session_id(session_id))
        self.assertEqual(len(session_id), 4 + 26)
        self.assertEqual(len(session_id[4:16]), 12)
        self.assertEqual(len(session_id[16:]), 14)

    def test_minted_id_decodes_to_current_time(self):
        now_ms = int(time.time() * 1000)
        session_id = standalone.mint_session_id(now_ms=now_ms, counter=7)
        decoded = standalone.decode_session_timestamp(session_id)
        modulus = 1 << 36
        delta = (now_ms % modulus) - decoded
        if delta > modulus // 2:
            delta -= modulus
        if delta < -modulus // 2:
            delta += modulus
        self.assertEqual(delta, 0)

    def test_minted_ids_are_unique(self):
        ids = {standalone.mint_session_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_rejects_wrong_shapes(self):
        for value in (
            "",
            None,
            "ses_",
            "ses_" + "0" * 25,
            "ses_" + "0" * 27,
            "ses_" + "z" * 12 + "A" * 14,
            "ses_" + "0" * 12 + "!" * 14,
            "ses_" + "0" * 12 + "A" * 13,
        ):
            self.assertFalse(standalone.is_session_id(value), value)

    def test_decode_returns_none_for_invalid(self):
        self.assertIsNone(standalone.decode_session_timestamp("not-a-session"))


class ClientDetectionTests(unittest.TestCase):
    def test_detects_opencode_headers(self):
        self.assertTrue(
            standalone.is_opencode_client({"x-opencode-client": "cli"})
        )
        self.assertTrue(
            standalone.is_opencode_client(
                {"x-opencode-session": standalone.mint_session_id()}
            )
        )
        self.assertTrue(
            standalone.is_opencode_client({"user-agent": "opencode/latest/2.1.1/cli"})
        )

    def test_detects_generic_clients(self):
        self.assertFalse(
            standalone.is_opencode_client(
                {"user-agent": "OpenAI/Python 1.99.1", "authorization": "Bearer x"}
            )
        )
        self.assertFalse(standalone.is_opencode_client({}))

    def test_requested_session_only_accepts_valid_ids(self):
        valid = standalone.mint_session_id()
        self.assertEqual(
            standalone.requested_session_id({"x-session-id": valid}), valid
        )
        self.assertIsNone(
            standalone.requested_session_id({"x-session-id": "ses_bogus"})
        )
        self.assertIsNone(standalone.requested_session_id({}))


class CompatibilityToolTests(unittest.TestCase):
    def test_adds_read_and_forbids_tool_calls_when_alone(self):
        payload = {}
        standalone.ensure_standalone_tools(payload)
        self.assertEqual(len(payload["tools"]), 1)
        self.assertEqual(payload["tools"][0]["function"]["name"], "read")
        self.assertEqual(payload["tool_choice"], "none")

    def test_keeps_client_tools_and_adds_read(self):
        payload = {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        }
        standalone.ensure_standalone_tools(payload)
        names = [tool["function"]["name"] for tool in payload["tools"]]
        self.assertEqual(names, ["get_weather", "read"])
        self.assertNotIn("tool_choice", payload)

    def test_does_not_duplicate_read(self):
        original = {
            "type": "function",
            "function": {
                "name": "read",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        payload = {"tools": [original], "tool_choice": "auto"}
        standalone.ensure_standalone_tools(payload)
        self.assertEqual(payload["tools"], [original])
        self.assertEqual(payload["tool_choice"], "auto")

    def test_replaces_invalid_tools_value(self):
        payload = {"tools": {"not": "a list"}}
        standalone.ensure_standalone_tools(payload)
        self.assertIsInstance(payload["tools"], list)


if __name__ == "__main__":
    unittest.main()
