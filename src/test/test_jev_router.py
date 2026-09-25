"""Offline adapter tests; intentionally separate from real API evaluation metrics."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import httpx
from langchain_core.messages import HumanMessage

from src.agent.harness import load_harness_config
from src.agent.jev_router import JevRouter, TASKS, build_review_router
from src.agent.review_policy import CHECKS, ReviewPolicy, ReviewRoute
from evals.review_routing.run import metrics, paired
from evals.review_routing.dataset import samples


def response(decision="skip", confidence=.9):
    choices = {v: float(v == decision) for v in ("skip", "review", "uncertain")}
    answers = {"decision": {"type": "choice", "choice": decision, "confidence": confidence, "probabilities": choices},
               "task_type": {"type": "choice", "choice": "default", "confidence": .9,
                             "probabilities": {k: float(k == "default") for k in TASKS}}}
    answers.update({k: {"type": "noul", "noul": 0.0} for k in CHECKS})
    return {"model": "jev-test", "answers": answers, "usage": {"input_tokens": 10, "output_tokens": 5}}


class JevTests(unittest.TestCase):
    def setUp(self):
        self.config = load_harness_config()
        self.state = {"messages": [HumanMessage(content="那第二家呢")]}

    def client(self, data=None, status=200):
        def handle(request):
            self.assertEqual(str(request.url), "https://api.typesafe.ai/v1/systemone")
            body = json.loads(request.content)
            self.assertEqual(body["state"]["recent_messages"][-1]["content"], "那第二家呢")
            self.assertEqual(len(body["questions"]), 7)
            return httpx.Response(status, json=response() if data is None else data)
        return JevRouter(api_key="offline-test", transport=httpx.MockTransport(handle))

    def test_sync_adapter_and_state_mapping(self):
        result = ReviewPolicy(self.config, self.client()).start(self.state)
        self.assertEqual(result["review_decision"]["decision"], "skip")

    def test_async_adapter(self):
        result = asyncio.run(ReviewPolicy(self.config, self.client(response("review"))).astart(self.state))
        self.assertEqual(result["review_decision"]["decision"], "review")
        self.assertIn("固定底线", result["rubric"])

    def test_uncertain_conservative(self):
        result = ReviewPolicy(self.config, self.client(response("uncertain"))).start(self.state)
        self.assertEqual(result["review_decision"]["decision"], "review")

    def test_low_confidence_only_when_configured(self):
        client = self.client(response("skip", .3))
        client.min_confidence = .8
        self.assertEqual(ReviewPolicy(self.config, client).start(self.state)["review_decision"]["decision"], "review")

    def test_malformed_and_http_errors_fail_closed(self):
        for status, data in ((401, {}), (429, {}), (529, {}), (200, {"answers": {}})):
            with self.subTest(status=status):
                result = ReviewPolicy(self.config, self.client(data, status)).start(self.state)
                self.assertEqual(result["review_decision"]["source"], "fallback")
                self.assertTrue(result["rubric"])

    def test_probability_validation(self):
        for values in ((.2, .2, .2), (float("nan"), 0, 0), (2, -1, 0)):
            data = response()
            data["answers"]["decision"]["probabilities"] = dict(zip(("skip", "review", "uncertain"), values))
            with self.assertRaises(ValueError):
                self.client().parse(data)

    def test_bad_choice_and_check(self):
        for field in ("decision", "task_type", "sources"):
            data = response()
            data["answers"][field] = {"type": "choice", "choice": "ignore_approval"}
            with self.assertRaises((ValueError, KeyError)):
                self.client().parse(data)

    def test_no_key_fails_closed(self):
        result = ReviewPolicy(self.config, JevRouter(api_key="")).start(self.state)
        self.assertEqual(result["review_decision"]["source"], "fallback")

    def test_async_timeout(self):
        async def slow(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json=response())
        client = JevRouter(api_key="test", transport=httpx.MockTransport(slow))
        self.config["review"]["router_timeout_seconds"] = .1
        result = asyncio.run(ReviewPolicy(self.config, client).astart(self.state))
        self.assertEqual(result["review_decision"]["source"], "fallback")

    def test_rules_and_explicit_do_not_call_api(self):
        client = JevRouter(api_key="")
        for state in ({"messages": [HumanMessage(content="你好")]}, {**self.state, "rubric": "explicit"}):
            result = ReviewPolicy(self.config, client).start(state)
            self.assertNotEqual(result["review_decision"]["source"], "fallback")

    def test_modes(self):
        for mode, expected in (("never", "skip"), ("always", "review")):
            self.config["review"]["mode"] = mode
            result = ReviewPolicy(self.config, JevRouter(api_key="")).start(self.state)
            self.assertEqual(result["review_decision"]["decision"], expected)

    def test_factory_and_schema(self):
        with patch.dict("os.environ", {"REVIEW_ROUTER_PROVIDER": "jev", "TYPESAFE_MODEL": "jev-test"}):
            self.assertEqual(build_review_router(self.config["review"]).model, "jev-test")
        with patch.dict("os.environ", {"REVIEW_ROUTER_PROVIDER": "invalid"}):
            with self.assertRaises(ValueError):
                build_review_router(self.config["review"])
        with self.assertRaises(ValueError):
            self.client().with_structured_output(dict)

    def test_metrics_include_fallback(self):
        rows = [{"label": "skip", "prediction": "review", "source": "fallback",
                 "meta": {"attempted": True, "valid_response": False, "call_ms": 8000, "error": "TimeoutError"}}]
        m = metrics(rows)
        self.assertEqual((m["fp"], m["fallbacks"], m["api_valid_rate"]), (1, 1, 0))

    def test_frozen_dataset_and_group_split(self):
        data = samples()
        self.assertEqual(len(data), 200)
        self.assertEqual(sum(s["split"] == "test" for s in data), 120)
        dev = {s["family"] for s in data if s["split"] == "dev"}
        test = {s["family"] for s in data if s["split"] == "test"}
        self.assertFalse(dev & test)
        self.assertEqual(len({s["id"] for s in data}), 200)
        self.assertEqual(paired([], "test")["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
