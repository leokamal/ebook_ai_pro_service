"""Summaries think less, timeouts are not retried and are named in the logs."""
import logging

import pytest

from app.core.config import get_settings
from app.services.llm import GeminiService, LLMTimeout, LLMUnavailable, _summary_options


@pytest.fixture(autouse=True)
def no_langchain(monkeypatch):
    monkeypatch.setattr(GeminiService, "_to_lc", staticmethod(lambda system, messages: [system, *messages]))


class Reply:
    content, usage_metadata = "ok [S1].", None


def service(model_name="gemini-3.5-flash", error=None, retries=1):
    built, calls = [], []

    class Model:
        def invoke(self, _):
            calls.append(1)
            if error:
                raise error
            return Reply()

        def stream(self, _):
            calls.append(1)
            if error:
                raise error
            yield Reply()

    def factory(**options):
        built.append(options)
        return Model()

    return GeminiService("k", model_name, max_retries=retries, model_factory=factory, sleep=lambda _s: None), built, calls


@pytest.mark.parametrize("model,options", [
    ("gemini-3.5-flash", {"thinking_level": "low"}),
    ("models/gemini-2.5-flash", {"thinking_budget": 0}),
    ("gemini-2.5-pro", {}),
])
def test_summary_options_by_model(model, options):
    assert _summary_options(model) == options


def test_only_summaries_get_the_thinking_setting():
    svc, built, _ = service()
    svc.generate("s", [])
    svc.generate("s", [], summarize=True)
    list(svc.stream("s", [], summarize=True))
    list(svc.stream("s", []))
    assert built == [{}, {"thinking_level": "low"}]  # one client each, reused


class ReadTimeout(Exception):
    pass


@pytest.mark.parametrize("call", [lambda s: s.generate("s", []), lambda s: list(s.stream("s", []))])
def test_timeout_is_not_retried_and_keeps_the_public_code(call, caplog):
    svc, _, calls = service(error=ReadTimeout("The read operation timed out"), retries=3)
    with caplog.at_level(logging.ERROR, logger="uvicorn.error"), pytest.raises(LLMTimeout) as e:
        call(svc)
    assert len(calls) == 1 and e.value.code == "llm_unavailable" and "llm_timeout" in caplog.text


def test_other_transient_errors_are_retried_once():
    svc, _, calls = service(error=Exception("503 unavailable"))
    with pytest.raises(LLMUnavailable) as e:
        svc.generate("s", [])
    assert len(calls) == 2 and not isinstance(e.value, LLMTimeout)


def test_defaults_and_env_override(monkeypatch):
    for var in ("LLM_TIMEOUT_SECONDS", "LLM_MAX_RETRIES"):
        monkeypatch.delenv(var, raising=False)
    assert (get_settings().llm_timeout, get_settings().llm_max_retries) == (180, 1)
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "90")
    assert get_settings().llm_timeout == 90


class Overloaded(Exception):
    def __str__(self):
        return "503 UNAVAILABLE high demand"


def scripted(*outcomes, fallback=""):
    """Each stream call takes the next outcome: an exception, "partial" (text then 503) or success."""
    script, calls = list(outcomes), []

    class Model:
        def stream(self, _):
            calls.append(1)
            step = script.pop(0)
            if isinstance(step, Exception):
                raise step
            yield Reply()
            if step == "partial":
                raise Overloaded()

    svc = GeminiService("k", "gemini-3.5-flash", max_retries=1, model_factory=lambda **_: Model(),
                        sleep=lambda _s: None, fallback_model=fallback)
    return svc, calls


def test_stream_is_retried_before_the_first_chunk():
    svc, calls = scripted(Overloaded(), "ok")
    assert list(svc.stream("s", [])) == ["ok [S1]."] and len(calls) == 2


def test_fallback_model_after_retries():
    svc, calls = scripted(Overloaded(), Overloaded(), "ok", fallback="gemini-3.1-flash-lite")
    assert list(svc.stream("s", [])) == ["ok [S1]."] and len(calls) == 3
    assert ("gemini-3.1-flash-lite", False) in svc._models
    svc, calls = scripted(*[Overloaded() for _ in range(4)], fallback="gemini-3.1-flash-lite")
    with pytest.raises(LLMUnavailable):
        list(svc.stream("s", []))
    assert len(calls) == 4


def test_stream_is_never_retried_after_output_was_sent():
    svc, calls = scripted("partial", "ok", fallback="gemini-3.1-flash-lite")
    out = []
    with pytest.raises(LLMUnavailable):
        for chunk in svc.stream("s", []):
            out.append(chunk)
    assert out == ["ok [S1]."] and len(calls) == 1
