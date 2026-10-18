"""One metered attempt via OpenAI Responses API; SDK-internal retries are disabled."""

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol

from openai import APIConnectionError, APIStatusError, OpenAI, OpenAIError
from pydantic import ValidationError

from app.ai.config import AIConfig
from app.ai.prompts import SYSTEM_PROMPT
from app.ai.schemas import AIReview, TokenUsage


@dataclass(frozen=True)
class ClientResult:
    review: AIReview | None
    usage: TokenUsage | None
    model: str
    reason: str = "completed"


class ReviewClientError(RuntimeError):
    def __init__(self, code: str, *, transient: bool = False, retry_after: float = 0) -> None:
        super().__init__(code)
        self.code = code
        self.transient = transient
        self.retry_after = retry_after


class ReviewClient(Protocol):
    def review(self, payload: str) -> ClientResult: ...


def retry_delay(value: str | None, now: datetime) -> float:
    if value is None:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if seconds < float("inf") else float("inf")
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value).astimezone(UTC) - now).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class OpenAIReviewClient:
    def __init__(self, api_key: str, config: AIConfig, *, sdk: OpenAI | None = None) -> None:
        self.config = config
        self.sdk = (
            sdk
            if sdk is not None
            else OpenAI(
                api_key=api_key,
                base_url="https://api.openai.com/v1",
                timeout=config.timeout_seconds,
                max_retries=0,
            )
        )

    def close(self) -> None:
        self.sdk.close()

    def review(self, payload: str) -> ClientResult:
        try:
            response = self.sdk.with_options(
                max_retries=0, timeout=self.config.timeout_seconds
            ).responses.parse(
                model=self.config.model,
                instructions=SYSTEM_PROMPT,
                input=[{"role": "user", "content": payload}],
                text_format=AIReview,
                max_output_tokens=self.config.max_output_tokens,
                tools=[],
                tool_choice="none",
                store=False,
                truncation="disabled",
                service_tier="default",
            )
            usage = None
            if response.usage is not None:
                usage = TokenUsage(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cached_input_tokens=response.usage.input_tokens_details.cached_tokens,
                )
            if response.status != "completed" or response.output_parsed is None:
                return ClientResult(None, usage, response.model, "incomplete_or_refused")
            # Validate again even if a custom SDK transport supplied the parsed value.
            review = AIReview.model_validate(response.output_parsed.model_dump())
            return ClientResult(review, usage, response.model)
        except APIConnectionError as exc:
            raise ReviewClientError("connection_error", transient=True) from exc
        except APIStatusError as exc:
            transient = exc.status_code in {408, 409, 429} or exc.status_code >= 500
            delay = retry_delay(exc.response.headers.get("retry-after"), datetime.now(UTC))
            retry_ms = exc.response.headers.get("retry-after-ms")
            if retry_ms is not None:
                delay = max(delay, retry_delay(retry_ms, datetime.now(UTC)) / 1000)
            raise ReviewClientError(
                f"http_{exc.status_code}", transient=transient, retry_after=delay
            ) from exc
        except (ValidationError, ValueError) as exc:
            raise ReviewClientError("invalid_response") from exc
        except OpenAIError as exc:
            raise ReviewClientError("sdk_error") from exc
