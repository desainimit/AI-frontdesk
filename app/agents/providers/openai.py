from openai import APIError, AsyncOpenAI
from openai.types.responses import ResponseInputParam
from pydantic import BaseModel, SecretStr, ValidationError

from app.agents.providers.base import LLMProviderError, LLMRequest, LLMResponse


class OpenAILLMProvider:
    def __init__(
        self,
        api_key: SecretStr,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._client = client or AsyncOpenAI(
            api_key=api_key.get_secret_value(), timeout=timeout_seconds, max_retries=0
        )
        self._model = model
        self._max_output_tokens = max_output_tokens

    @staticmethod
    def _messages(request: LLMRequest) -> ResponseInputParam:
        return [{"role": message.role, "content": message.content} for message in request.messages]

    async def generate(self, request: LLMRequest) -> LLMResponse:
        try:
            response = await self._client.responses.create(
                model=self._model,
                input=self._messages(request),
                store=False,
                max_output_tokens=self._max_output_tokens,
            )
            if response.status != "completed" or not response.output_text:
                raise LLMProviderError("LLM returned no complete response")
            return LLMResponse(response.output_text)
        except APIError:
            raise LLMProviderError("LLM request failed") from None

    async def generate_structured[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> T:
        try:
            response = await self._client.responses.parse(
                model=self._model,
                input=self._messages(request),
                text_format=schema,
                store=False,
                max_output_tokens=self._max_output_tokens,
            )
            if response.status != "completed" or response.output_parsed is None:
                raise LLMProviderError("LLM returned no valid structured response")
            return schema.model_validate_json(response.output_parsed.model_dump_json())
        except (APIError, ValidationError, ValueError):
            raise LLMProviderError("LLM structured request failed") from None

    async def close(self) -> None:
        await self._client.close()
