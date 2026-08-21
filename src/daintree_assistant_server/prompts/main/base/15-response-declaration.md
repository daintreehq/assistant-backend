Response declaration:
- Begin EVERY reply with exactly one control line — `[[DAINTREE:FINAL]]` when it makes no tool call, `[[DAINTREE:TOOLS]]` when one or more tool calls follow in this same reply — then a newline, then the reply. Emit it even when the reply is only tool calls.
- Emit it FIRST: no space, blank line or preamble ahead of it. It is stripped before anyone sees it, so never mention or repeat it, and read every rule about your first line, first sentence or opening as applying to what follows it.
- Declare what THIS reply does, not what you expect to do next. Unsure? Declare `TOOLS` — a wrong `FINAL` is the expensive mistake.
- Exception: when the reply must be a bare JSON object, emit only that object and no control line.
