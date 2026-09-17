"""HTTPX transport with explicit connection ownership and normalized failures."""

from collections.abc import AsyncIterator

import httpx

from ..domain import ConnectFailure, Endpoint, GatewayError, Payload
from ..ports import BackendResponse


class HttpxResponse:
    """Adapt a streamed HTTPX response to the application port."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def status(self) -> int:
        """Return the HTTP status."""
        return self._response.status_code

    @property
    def headers(self) -> dict[str, str]:
        """Return normalized headers."""
        return dict(self._response.headers)

    async def chunks(self) -> AsyncIterator[bytes]:
        """Read decoded bytes and translate transport failures."""
        try:
            async for chunk in self._response.aiter_bytes():
                yield chunk
        except httpx.HTTPError:
            raise GatewayError("upstream_read_failed") from None

    async def close(self) -> None:
        """Release the underlying response."""
        await self._response.aclose()


class HttpxTransport:
    """Forward a single attempt using an injected pooled client."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def send(
        self, endpoint: Endpoint, request: Payload, headers: dict[str, str]
    ) -> BackendResponse:
        """Open a response without following redirects or automatically retrying."""
        outgoing = headers | {"content-type": "application/json", "accept-encoding": "identity"}
        if endpoint.api_key:
            outgoing["authorization"] = f"Bearer {endpoint.api_key}"
        try:
            req = self.client.build_request(
                "POST",
                endpoint.base_url.rstrip("/") + "/chat/completions",
                json=request,
                headers=outgoing,
            )
            return HttpxResponse(await self.client.send(req, stream=True))
        except httpx.ConnectError, httpx.ConnectTimeout:
            raise ConnectFailure() from None
        except httpx.TimeoutException:
            raise GatewayError("upstream_timeout", 504) from None
        except httpx.HTTPError:
            raise GatewayError("upstream_transport_failed") from None
