"""Create a local SSE server that proxies requests to a stdio MCP server."""

from dataclasses import dataclass
from typing import Literal

import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

from .proxy_server import create_proxy_server


@dataclass
class SseServerSettings:
    """Settings for the server."""

    bind_host: str
    port: int
    allow_origins: list[str] | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    auth_token: str | None = None  # Add this line


def create_starlette_app(
    mcp_server: Server[object],
    *,
    allow_origins: list[str] | None = None,
    debug: bool = False,
    auth_token: str | None = None,  # Add this parameter
) -> Starlette:
    """Create a Starlette application that can server the provied mcp server with SSE."""
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> None:
        async with sse.connect_sse(
            request.scope,
            request.receive,
            request._send,  # noqa: SLF001
        ) as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

    middleware: list[Middleware] = []
    if allow_origins is not None:
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=allow_origins,
                allow_methods=["*"],
                allow_headers=["*"],
            ),
        )

    # Add authentication middleware
    @app.middleware("http")
    async def auth_middleware(request, call_next):
        # Skip auth check if no token is configured
        if auth_token is None:
            return await call_next(request)
            
        # Get the authorization header
        auth_header = request.headers.get("Authorization")
        
        # Check if header exists and matches expected format
        if not auth_header or not auth_header.startswith("Bearer "):
            return Response(
                "Unauthorized: Missing or invalid Authorization header",
                status_code=401
            )
            
        # Extract and validate the token
        token = auth_header.replace("Bearer ", "")
        if token != auth_token:
            return Response(
                "Unauthorized: Invalid token",
                status_code=401
            )
            
        # If we get here, auth is successful
        return await call_next(request)

    return Starlette(
        debug=debug,
        middleware=middleware,
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )


async def run_sse_server(
    stdio_params: StdioServerParameters,
    sse_settings: SseServerSettings,
) -> None:
    """Run the stdio client and expose an SSE server.

    Args:
        stdio_params: The parameters for the stdio client that spawns a stdio server.
        sse_settings: The settings for the SSE server that accepts incoming requests.

    """
    async with stdio_client(stdio_params) as streams, ClientSession(*streams) as session:
        mcp_server = await create_proxy_server(session)

        # Bind SSE request handling to MCP server
        starlette_app = create_starlette_app(
            mcp_server,
            allow_origins=sse_settings.allow_origins,
            debug=(sse_settings.log_level == "DEBUG"),
            auth_token=sse_settings.auth_token,  # Pass the auth_token parameter
        )

        # Configure HTTP server
        config = uvicorn.Config(
            starlette_app,
            host=sse_settings.bind_host,
            port=sse_settings.port,
            log_level=sse_settings.log_level.lower(),
        )
        http_server = uvicorn.Server(config)
        await http_server.serve()
