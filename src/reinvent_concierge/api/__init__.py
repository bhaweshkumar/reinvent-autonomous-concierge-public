"""AWS Events API access: REST client, wire mapping, rate limiting, hosted MCP client."""

from .client import AWSEventsRestClient, SessionResolver

__all__ = ["AWSEventsRestClient", "SessionResolver"]
