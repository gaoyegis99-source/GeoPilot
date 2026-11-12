"""Validate outbound requests and bound retries of model suggestions."""

from collections.abc import Iterable

from geopilot.llm.base import ModelErrorCode, ModelProvider, ModelProviderError, ModelRequest, validate_model_timeout
from geopilot.llm.data_boundary import DataBoundaryError, check_outbound_request
from geopilot.llm.response_parser import ParsedModelSuggestion, parse_model_response


def _checked_request_json(request: ModelRequest, *, protected_literals: Iterable[str] = ()) -> str:
    if not isinstance(request, ModelRequest):
        raise TypeError("A ModelRequest is required")
    try:
        serialized = ModelRequest.model_validate(request).model_dump_json()
    except (ValueError, RecursionError, OverflowError):
        raise DataBoundaryError("$") from None
    check_outbound_request(serialized, protected_literals=protected_literals)
    return serialized


def request_model_suggestion(
    provider: ModelProvider,
    request: ModelRequest,
    *,
    timeout: float = 30.0,
    max_attempts: int = 2,
    protected_literals: Iterable[str] = (),
) -> ParsedModelSuggestion:
    """Send independent copies of the checked request and validate the first reply."""
    seconds = validate_model_timeout(timeout)
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts must be an integer from 1 to 3")
    serialized = _checked_request_json(request, protected_literals=protected_literals)

    for attempt in range(1, max_attempts + 1):
        candidate = ModelRequest.model_validate_json(serialized)
        try:
            response = provider.generate(candidate, timeout=seconds)
        except ModelProviderError as error:
            failure = ModelProviderError(error.code)
            if not failure.retryable or attempt == max_attempts:
                raise failure from None
        except DataBoundaryError:
            raise
        except Exception:
            raise ModelProviderError(ModelErrorCode.INVALID_RESPONSE) from None
        else:
            return parse_model_response(response, request=ModelRequest.model_validate_json(serialized))
