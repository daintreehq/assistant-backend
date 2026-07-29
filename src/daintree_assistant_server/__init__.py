"""Daintree Assistant Server.

A Daintree-operated model-orchestration service exposing a Daintree-native API
(``POST /v1/daintree/respond``) in which the session/skill envelope is first-class
and skills are their own response block. It speaks the OpenAI wire shape only
INTERNALLY, to upstream providers, behind the ``ChatProvider`` abstraction.
"""

__version__ = "1.0.0"

# The Daintree extension protocol version implemented by this server.
PROTOCOL_VERSION = 2
PROTOCOL_MIN = 2
PROTOCOL_MAX = 2
