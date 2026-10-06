from types import SimpleNamespace

from flows import llm


def test_request_shape_follows_the_model_family():
    msgs = [{"role": "user", "content": "hi"}]
    new = llm.build_request("gpt-6-luna", msgs, max_tokens=100, json_mode=True)
    assert new["reasoning_effort"] == "low" and new["max_completion_tokens"] == 100
    assert "temperature" not in new and new["response_format"] == {"type": "json_object"}

    old = llm.build_request("gpt-4.1-mini", msgs, max_tokens=100, temperature=0.3)
    assert old["temperature"] == 0.3 and old["max_tokens"] == 100
    assert "reasoning_effort" not in old


class _Client:
    def __init__(self, fail_flex=False):
        self.calls = []
        self.fail_flex = fail_flex
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if kw.get("service_tier") == "flex" and self.fail_flex:
            raise RuntimeError("Flex is unavailable right now")
        return SimpleNamespace(tier=kw.get("service_tier"))


def test_flex_first_then_standard_when_flex_refuses():
    client = _Client(fail_flex=True)
    req = llm.build_request("gpt-6.1-sol", [], max_tokens=10)
    resp = llm.complete(client, req)
    assert [c.get("service_tier") for c in client.calls] == ["flex", None]
    assert resp.tier is None


def test_models_without_flex_go_straight_to_standard():
    client = _Client()
    llm.complete(client, llm.build_request("gpt-4.1-mini", [], max_tokens=10))
    assert client.calls[0].get("service_tier") is None
    assert len(client.calls) == 1


def test_cost_uses_the_price_table_and_halves_on_flex():
    usage = SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=100_000)
    assert llm.usage_cost("gpt-6.1-sol", usage, flex=False) == 3.0     # 2 + 1
    assert llm.usage_cost("gpt-6.1-sol", usage, flex=True) == 1.5
    assert llm.usage_cost("gpt-4.1-mini", usage, flex=True) == 0.56   # no flex for 4.1: full price
    assert llm.usage_cost("unknown-model", usage) == 0.0
    responses_usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    assert llm.total_tokens(responses_usage) == 15
