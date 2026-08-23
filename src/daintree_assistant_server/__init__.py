"""Daintree Assistant Server.

A Daintree-operated model-orchestration service exposing a Daintree-native API
(``POST /v1/daintree/respond``) in which the session/runbook envelope is first-class
and runbooks are their own response block. It speaks the OpenAI wire shape only
INTERNALLY, to upstream providers, behind the ``ChatProvider`` abstraction.
"""

__version__ = "1.0.0"

# The Daintree extension protocol version implemented by this server.
#
# 3 is the runbook protocol. It is a HARD break from 2, not an addition: the
# response block, the selection fields, the routes and the warning codes all say
# "runbook" where 2 said "skill", and nothing answers to the old spelling. A
# protocol-2 client is therefore rejected at the door rather than served a body it
# cannot parse — which is the whole reason the negotiated version exists
# separately from the `/v1/` URL prefix.
PROTOCOL_VERSION = 3
PROTOCOL_MIN = 3
PROTOCOL_MAX = 3
