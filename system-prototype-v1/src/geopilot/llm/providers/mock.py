"""A fixed in-process model provider for local contract and workflow tests."""

from geopilot.llm.base import ModelErrorCode, ModelProviderError, ModelRequest, ModelResponse, validate_model_timeout


class MockModelProvider:
    def __init__(self, *, response_text: str | None = None, error_code: ModelErrorCode | str | None = None) -> None:
        if (response_text is None) == (error_code is None):
            raise ValueError("Configure exactly one model response or error code")
        self._response = ModelResponse(text=response_text) if response_text is not None else None
        self._error_code = ModelProviderError(error_code).code if error_code is not None else None

    def generate(self, request: ModelRequest, *, timeout: float) -> ModelResponse:
        validate_model_timeout(timeout)
        if not isinstance(request, ModelRequest):
            raise TypeError("A ModelRequest is required")
        ModelRequest.model_validate(request)
        if self._error_code is not None:
            raise ModelProviderError(self._error_code)
        assert self._response is not None
        return self._response
