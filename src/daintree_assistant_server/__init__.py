"""Daintree Assistant Server.

A Daintree-operated model orchestration service exposing a largely
OpenAI-compatible Chat Completions API plus a negotiated Daintree extension.
"""

__version__ = "1.0.0"

# The Daintree extension protocol version implemented by this server.
PROTOCOL_VERSION = 1
PROTOCOL_MIN = 1
PROTOCOL_MAX = 1
