"""Deterministic providers mounted only by the Compose browser-test overlay."""

from __future__ import annotations

from dataclasses import replace

from apps.api.main import create_app
from config.settings import Settings, get_settings
from src.llm.base import LLMResponse


class BrowserTestEmbedder:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [
            [
                1.0 if "policy" in text.lower() else 0.5,
                1.0 if "escalat" in text.lower() or "review" in text.lower() else 0.25,
                float(len(text)) / 1_000,
            ]
            for text in texts
        ]


class BrowserTestLLM:
    provider = "deterministic-browser-test"
    model = "deterministic-browser-test-v1"
    configured = True

    def generate(self, prompt: str) -> LLMResponse:
        if "Classify a business-intelligence question" in prompt:
            if "Which uploaded transactions" in prompt:
                text = '{"route":"hybrid","confidence":1}'
            elif "policy" in prompt.lower():
                text = '{"route":"rag","confidence":1}'
            else:
                text = '{"route":"sql","confidence":1}'
        elif "Extract business criteria" in prompt:
            text = '{"thresholds":[1000]}'
        elif "careful document analyst" in prompt:
            text = 'The policy states "Transactions over $1,000 require manager review." Source 1'
        else:
            text = (
                'SELECT "merchant", "amount" FROM "uploaded_data" '
                'WHERE "amount" > 1000 ORDER BY "amount" DESC'
            )
        return LLMResponse(text=text, model=self.model, provider=self.provider)


def _llm_factory(_settings: Settings) -> BrowserTestLLM:
    return BrowserTestLLM()


app = create_app(
    settings=replace(
        get_settings(),
        gemini_api_key="browser-test-not-a-credential",
        gemini_model=BrowserTestLLM.model,
        embedding_model="deterministic-browser-test",
        local_only_mode=False,
        retrieval_max_distance=None,
    ),
    embedder_factory=lambda _model: BrowserTestEmbedder(),
    llm_client_factory=_llm_factory,
)
