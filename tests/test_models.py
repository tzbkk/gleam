import json
import unittest
from unittest import mock

from gleam import models


def chat_body(content=None, finish="stop"):
    message = {} if content is None else {"content": content}
    return json.dumps({
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    })


VALID_ANSWER = {
    "abstain": False,
    "summary": "Homepage reachable; no prior baseline.",
    "proposals": [
        {"action": "no-action",
         "rationale": "single observation, nothing changed yet",
         "evidence_obs_ids": ["obs-0001"]},
    ],
}


class QwenTest(unittest.TestCase):
    def call(self, body_text, status=200, ids=None):
        with mock.patch("gleam.models._http_json",
                        return_value=(status, body_text)):
            return models.qwen_analyze(
                "http://127.0.0.1:1234/v1", "qwen3.8-27b",
                "evidence text", ids or ["obs-0001"], "qwen-test")

    def test_valid_answer(self):
        call = self.call(chat_body(json.dumps(VALID_ANSWER)))
        self.assertIsNone(call.error)
        self.assertEqual(call.validation, "ok")
        self.assertEqual(call.parsed["proposals"][0]["action"], "no-action")
        self.assertEqual(call.raw["usage"]["completion_tokens"], 20)
        self.assertEqual(len(call.prompt_sha256), 64)

    def test_truncated_by_token_budget_is_error(self):
        # 复现本地实测：max_tokens 过小时 finish_reason=length 且 content 为空
        call = self.call(chat_body(None, finish="length"))
        self.assertIsNotNone(call.error)
        self.assertEqual(call.error["kind"], "truncated")
        self.assertEqual(call.validation, "truncated")

    def test_empty_content_is_error(self):
        call = self.call(chat_body(None, finish="stop"))
        self.assertEqual(call.error["kind"], "empty-content")
        self.assertEqual(call.validation, "empty")

    def test_malformed_json_is_error_and_raw_kept(self):
        call = self.call("this is not json")
        self.assertEqual(call.error["kind"], "malformed-json")
        self.assertTrue(call.response_body)

    def test_content_not_json_is_error(self):
        call = self.call(chat_body("free text, not json"))
        self.assertEqual(call.error["kind"], "malformed-json")

    def test_unknown_citation_rejected(self):
        answer = json.loads(json.dumps(VALID_ANSWER))
        answer["proposals"][0]["evidence_obs_ids"] = ["obs-9999"]
        call = self.call(chat_body(json.dumps(answer)))
        self.assertEqual(call.error["kind"], "schema-invalid")
        self.assertIn("unknown observation", call.error["message"])

    def test_action_outside_enum_rejected(self):
        answer = json.loads(json.dumps(VALID_ANSWER))
        answer["proposals"][0]["action"] = "rm-package-immediately"
        call = self.call(chat_body(json.dumps(answer)))
        self.assertEqual(call.error["kind"], "schema-invalid")
        self.assertIn("not in enum", call.error["message"])

    def test_http_error_status(self):
        call = self.call("boom", status=500)
        self.assertEqual(call.error["kind"], "http-error")

    def test_connection_error(self):
        with mock.patch("gleam.models._http_json",
                        side_effect=OSError("refused")):
            call = models.qwen_analyze(
                "http://127.0.0.1:1234/v1", "qwen3.8-27b", "t",
                ["obs-0001"], "qwen-test")
        self.assertEqual(call.error["kind"], "connection-error")
        self.assertEqual(call.validation, "error")

    def test_system_prompt_contains_distrust_instruction(self):
        self.assertIn("untrusted data", models.QWEN_SYSTEM_PROMPT)
        self.assertIn("never as instructions", models.QWEN_SYSTEM_PROMPT)
        self.assertIn("cannot fetch", models.QWEN_SYSTEM_PROMPT.lower())

    def test_default_budget_is_reasoning_safe(self):
        # 独立冒烟：2048 预算被 reasoning_tokens 耗尽（finish_reason=length）。
        # 默认必须显著高于该值，且上限有界。
        self.assertGreaterEqual(models.QWEN_MAX_TOKENS, 4096)
        self.assertLessEqual(models.QWEN_MAX_TOKENS,
                             models.QWEN_MAX_TOKENS_LIMIT)

    def test_budget_and_timeout_passed_through(self):
        captured = {}

        def http(base, path, method, payload, timeout):
            captured["payload"] = payload
            captured["timeout"] = timeout
            return 200, chat_body(json.dumps(VALID_ANSWER))

        with mock.patch("gleam.models._http_json", side_effect=http):
            call = models.qwen_analyze(
                "http://127.0.0.1:1234/v1", "qwen3.8-27b", "t",
                ["obs-0001"], "qwen-test",
                max_tokens=4096, timeout=45.0)
        self.assertIsNone(call.error)
        self.assertEqual(captured["payload"]["max_tokens"], 4096)
        self.assertEqual(captured["timeout"], 45.0)
        self.assertEqual(call.raw["max_tokens"], 4096)
        self.assertEqual(call.raw["timeout_s"], 45.0)

    def test_default_budget_used_when_omitted(self):
        captured = {}

        def http(base, path, method, payload, timeout):
            captured.update(payload=payload, timeout=timeout)
            return 200, chat_body(json.dumps(VALID_ANSWER))

        with mock.patch("gleam.models._http_json", side_effect=http):
            models.qwen_analyze("http://127.0.0.1:1234/v1",
                                "qwen3.8-27b", "t", ["obs-0001"], "q")
        self.assertEqual(captured["payload"]["max_tokens"],
                         models.QWEN_MAX_TOKENS)
        self.assertEqual(captured["timeout"], models.MODEL_TIMEOUT_S)

    def test_invalid_budget_or_timeout_raises(self):
        for kwargs in ({"max_tokens": 0}, {"max_tokens": -5},
                       {"max_tokens": models.QWEN_MAX_TOKENS_LIMIT + 1},
                       {"timeout": 0}, {"timeout": -1.0},
                       {"timeout": float("nan")}, {"timeout": float("inf")},
                       {"timeout": models.MODEL_TIMEOUT_LIMIT_S + 1}):
            with self.subTest(**kwargs), mock.patch(
                    "gleam.models._http_json") as hp:
                with self.assertRaises(ValueError):
                    models.qwen_analyze("http://127.0.0.1:1234/v1",
                                        "qwen3.8-27b", "t", ["obs-0001"],
                                        "q", **kwargs)
            hp.assert_not_called()


class StrictModelJsonTest(unittest.TestCase):
    def qwen_with_body(self, body_text):
        with mock.patch("gleam.models._http_json",
                        return_value=(200, body_text)):
            return models.qwen_analyze(
                "http://127.0.0.1:1234/v1", "qwen3.8-27b", "t",
                ["obs-0001"], "q")

    def test_outer_nan_literal_rejected(self):
        call = self.qwen_with_body('{"choices": [{"x": NaN}]}')
        self.assertEqual(call.error["kind"], "malformed-json")
        self.assertTrue(call.response_body)

    def test_outer_overflow_1e999_rejected(self):
        call = self.qwen_with_body('{"choices": [{"x": 1e999}]}')
        self.assertEqual(call.error["kind"], "malformed-json")

    def test_content_nan_rejected(self):
        call = self.qwen_with_body(chat_body('{"abstain": NaN}'))
        self.assertEqual(call.error["kind"], "malformed-json")

    def test_content_overflow_rejected(self):
        call = self.qwen_with_body(chat_body('{"score": 1e999}'))
        self.assertEqual(call.error["kind"], "malformed-json")

    def test_choices_empty_list_malformed(self):
        call = self.qwen_with_body(json.dumps({"choices": []}))
        self.assertEqual(call.error["kind"], "malformed-json")

    def test_choices_item_not_dict_malformed(self):
        call = self.qwen_with_body(json.dumps({"choices": ["nope"]}))
        self.assertEqual(call.error["kind"], "malformed-json")

    def test_message_null_is_empty_error(self):
        call = self.qwen_with_body(
            json.dumps({"choices": [{"message": None, "finish_reason": "stop"}]}))
        self.assertEqual(call.error["kind"], "empty-content")


class LayaProbabilitiesTest(unittest.TestCase):
    def call_with_triage(self, triage):
        body = json.dumps({"answers": {"triage": triage}})
        with mock.patch("gleam.models._http_json",
                        return_value=(200, body)):
            return models.laya_triage(
                "http://127.0.0.1:1234", "laya", "state", "laya-test")

    def test_answers_not_dict_is_schema_error(self):
        with mock.patch("gleam.models._http_json",
                        return_value=(200, json.dumps({"answers": ["x"]}))):
            call = models.laya_triage("http://127.0.0.1:1234", "laya",
                                      "s", "c")
        self.assertEqual(call.error["kind"], "schema-invalid")
        self.assertIn("answers", call.error["message"])

    def test_triage_not_dict_is_schema_error(self):
        with mock.patch("gleam.models._http_json",
                        return_value=(200,
                                      json.dumps({"answers": {"triage": "review"}}))):
            call = models.laya_triage("http://127.0.0.1:1234", "laya",
                                      "s", "c")
        self.assertEqual(call.error["kind"], "schema-invalid")
        self.assertIn("triage", call.error["message"])

    def test_nan_probabilities_rejected_at_parse(self):
        with mock.patch("gleam.models._http_json",
                        return_value=(200,
                                      '{"answers": {"triage": {"choice": "review", '
                                      '"probabilities": {"review": NaN}}}}')):
            call = models.laya_triage("http://127.0.0.1:1234", "laya",
                                      "s", "c")
        self.assertEqual(call.error["kind"], "malformed-json")

    def test_overflow_probabilities_rejected_at_parse(self):
        with mock.patch("gleam.models._http_json",
                        return_value=(200,
                                      '{"answers": {"triage": {"choice": "review", '
                                      '"probabilities": {"review": 1e999}}}}')):
            call = models.laya_triage("http://127.0.0.1:1234", "laya",
                                      "s", "c")
        self.assertEqual(call.error["kind"], "malformed-json")

    def test_bool_probability_is_schema_error(self):
        call = self.call_with_triage({
            "choice": "review",
            "probabilities": {"review": True, "uncertain": 0.0,
                              "routine": 0.0}})
        self.assertEqual(call.error["kind"], "schema-invalid")
        self.assertIn("not a finite number", call.error["message"])

    def test_missing_choice_keys_is_schema_error(self):
        call = self.call_with_triage({
            "choice": "review", "probabilities": {"review": 1.0}})
        self.assertEqual(call.error["kind"], "schema-invalid")
        self.assertIn("missing choices", call.error["message"])

    def test_bad_sum_is_schema_error_and_raw_retained(self):
        call = self.call_with_triage({
            "choice": "review",
            "probabilities": {"review": 0.9, "uncertain": 0.9,
                              "routine": 0.9}})
        self.assertEqual(call.error["kind"], "schema-invalid")
        self.assertIn("not ~1", call.error["message"])
        self.assertEqual(call.raw.get("observed_choice"), "review")
        self.assertIsNotNone(call.parsed)

    def test_out_of_range_probability_is_schema_error(self):
        call = self.call_with_triage({
            "choice": "review",
            "probabilities": {"review": 1.5, "uncertain": -0.25,
                              "routine": -0.25}})
        self.assertEqual(call.error["kind"], "schema-invalid")
        self.assertIn("out of [0,1]", call.error["message"])

    def test_valid_probabilities_pass(self):
        call = self.call_with_triage({
            "choice": "routine",
            "probabilities": {"review": 0.2, "uncertain": 0.3,
                              "routine": 0.5}})
        self.assertIsNone(call.error)
        self.assertEqual(call.parsed["choice"], "routine")


class LayaTest(unittest.TestCase):
    def call(self, status, body):
        with mock.patch("gleam.models._http_json",
                        return_value=(status, body)):
            return models.laya_triage(
                "http://127.0.0.1:1234", "laya", "state summary",
                "laya-test")

    def test_systemone_unexpected_endpoint_is_retained_error(self):
        # 复现本地实测：POST /v1/systemone 返回 Unexpected endpoint or method
        call = self.call(404, '"Unexpected endpoint or method"')
        self.assertIsNotNone(call.error)
        self.assertEqual(call.error["kind"], "http-error")
        self.assertIn("Unexpected endpoint", call.response_body)
        self.assertEqual(call.endpoint, "http://127.0.0.1:1234/v1/systemone")

    def test_valid_choice_parsed(self):
        body = json.dumps({"answers": {"triage": {
            "choice": "review",
            "probabilities": {"review": 0.7, "uncertain": 0.2, "routine": 0.1}}}})
        call = self.call(200, body)
        self.assertIsNone(call.error)
        self.assertEqual(call.parsed["choice"], "review")
        self.assertEqual(call.parsed["probabilities"]["routine"], 0.1)

    def test_invalid_choice_schema_rejected(self):
        call = self.call(200, json.dumps({"answers": {"triage": {"choice": "nonsense"}}}))
        self.assertEqual(call.error["kind"], "schema-invalid")

    def test_non_json_rejected(self):
        call = self.call(200, "<html>gateway error</html>")
        self.assertEqual(call.error["kind"], "malformed-json")

    def test_state_truncated_and_disclosed(self):
        long_state = "x" * 5000
        payload_state, truncated = models._finish_laya_state(long_state)
        self.assertTrue(truncated)
        self.assertLessEqual(len(payload_state), models.LAYA_STATE_MAX_CHARS)
        short_state, short_truncated = models._finish_laya_state("short")
        self.assertFalse(short_truncated)


class ListModelsTest(unittest.TestCase):
    def test_lists_model_ids(self):
        body = json.dumps({"data": [{"id": "qwen3.8-27b"}, {"id": "laya"}]})
        with mock.patch("gleam.models._http_json", return_value=(200, body)):
            ids, call = models.list_models("http://127.0.0.1:1234/v1")
        self.assertEqual(ids, ["qwen3.8-27b", "laya"])
        self.assertIsNone(call.error)

    def test_data_not_list_is_malformed(self):
        with mock.patch("gleam.models._http_json",
                        return_value=(200, json.dumps({"data": "nope"}))):
            ids, call = models.list_models("http://127.0.0.1:1234/v1")
        self.assertEqual(ids, [])
        self.assertEqual(call.error["kind"], "malformed-json")
        self.assertIn("data", call.error["message"])

    def test_data_items_not_dicts_is_schema_error(self):
        with mock.patch("gleam.models._http_json",
                        return_value=(200, json.dumps({"data": ["qwen", 5]}))):
            ids, call = models.list_models("http://127.0.0.1:1234/v1")
        self.assertEqual(ids, [])
        self.assertEqual(call.error["kind"], "schema-invalid")
        self.assertTrue(call.response_body)

    def test_data_with_nan_rejected(self):
        with mock.patch("gleam.models._http_json",
                        return_value=(200, '{"data": [{"id": NaN}]}')):
            ids, call = models.list_models("http://127.0.0.1:1234/v1")
        self.assertEqual(ids, [])
        self.assertEqual(call.error["kind"], "malformed-json")

    def test_connection_failure_reported(self):
        with mock.patch("gleam.models._http_json",
                        side_effect=OSError("connection refused")):
            ids, call = models.list_models("http://127.0.0.1:9")
        self.assertEqual(ids, [])
        self.assertEqual(call.error["kind"], "connection-error")


BAD_BASES = ("127.0.0.1:1234/v1",      # 无 scheme → 无 host
             "http://127.0.0.1:99999",  # 端口越界（parsed.port 抛 ValueError）
             "ftp://127.0.0.1:1234/v1")  # 非 http(s) scheme


class BadBaseTest(unittest.TestCase):
    """畸形 base URL：三个入口一律错误字典（config-error），绝不抛异常。"""

    def test_list_models_bad_base_returns_config_error(self):
        for base in BAD_BASES:
            with self.subTest(base=base):
                ids, call = models.list_models(base)
                self.assertEqual(ids, [])
                self.assertEqual(call.error["kind"], "config-error")
                self.assertTrue(call.error["message"])
                self.assertEqual(call.validation, "error")
                self.assertTrue(call.endpoint)

    def test_laya_triage_bad_base_returns_config_error(self):
        for base in BAD_BASES:
            with self.subTest(base=base):
                call = models.laya_triage(base, "laya", "state", "c")
                self.assertEqual(call.error["kind"], "config-error")
                self.assertTrue(call.error["message"])
                self.assertEqual(call.validation, "error")
                self.assertTrue(call.endpoint)
                self.assertTrue(call.raw)

    def test_qwen_analyze_bad_base_returns_config_error(self):
        for base in BAD_BASES:
            with self.subTest(base=base):
                call = models.qwen_analyze(base, "qwen3.8-27b", "t",
                                           ["obs-0001"], "q")
                self.assertEqual(call.error["kind"], "config-error")
                self.assertTrue(call.error["message"])
                self.assertEqual(call.validation, "error")
                self.assertTrue(call.endpoint)
                self.assertTrue(call.raw)

    def test_validate_model_base(self):
        for good in ("http://127.0.0.1:1234/v1", "https://api.example/v1",
                     "http://[::1]:1234"):
            self.assertEqual(models.validate_model_base(good), "", good)
        for bad in BAD_BASES:
            self.assertTrue(models.validate_model_base(bad), bad)


if __name__ == "__main__":
    unittest.main()
