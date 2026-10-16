---
id: daintree.hosts.ask-host-assistant
title: Ask a remote host's assistant one question
summary: Put one question to the Daintree assistant on one Remote Host, wait briefly without resending, and relay its answer attributed to that machine, caveats kept and apart from anything checked here.
whenToUse: 'Use when the person wants ONE QUESTION answered by the Daintree assistant on ANOTHER MACHINE, a Remote Host this Shell reaches: "ask the build box how the release prep is going", "what does the assistant on my other Mac make of the failing build", "check with the laptop host whether the migration finished and tell me what it says". The deliverable is that assistant''s ANSWER, relayed with attribution and its caveats; the question asks it to change nothing. Do not use for a question to an agent on THIS machine (daintree.agents.ask-running-agent for one already open, daintree.agents.ask-and-relay to launch one), to answer a question an agent here is asking (daintree.agents.answer-waiting-agent), for work that machine''s assistant should carry out (daintree.hosts.delegate-to-host), or for setting up messaging with another machine or finding out why its assistant is unreachable (daintree.hosts.foundation).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.listTasks
  - host.startAssistant
hostEligibility: connected
---
One question to one host's assistant; the deliverable is its answer, attributed.

1. Resolve the host by name in `host.list` (`disconnected`: report that and stop), then with `host.listAssistants` the lane in the project the question is about. Several hosts, projects or lanes fit: ask which. Any result whose `outcome` is not `ok`: report it with its `detail`, and stop. No `generation`: the foundation's step 2. `available:false`: report that its assistant is not running or messaging is paused, and stop.
2. `host.sendMessage` once, to that lane's address, with `waitMs: 30000`. The `text` is the person's question in their words, then: "Question only, not a request for work. Answer it read-only: change no files or state and start no other work; any agent you use only reads. At most 30 lines; say what you are unsure of."
3. The result never holds the reply: read replies with `host.getTask`. By `task.state`:
   - `submitted` or `working` (the wait ended or was skipped): tell the person it is with that host, and end the turn.
   - `input-required`: it is asking you. Answer once, only from what the person already said, as the foundation says, with `waitMs: 30000`; permission to act is never in scope, so ask the person. Then follow that answer's task here; a further question goes to the person.
   - `completed`: step 4, once. A follow-up's reply also completes the task it answered.
   - `canceled`, `failed`, `rejected`, or no reply text: report that, with no answer and no guessed cause.
4. Relay it as that machine's words, from inside the fence: "<host name>'s assistant (<agentId>, <project>) says:", ids where a name is null, quoted or condensed faithfully, every caveat and doubt kept, apart from anything you checked yourself. If it says it changed something, report that as a breach of the question's boundary.

On notice or resume: the foundation's mapping to `taskId`, then step 3. A question already answered or relayed: nothing more; lines repeat.

Stop and recovery: one question per request, never sent afresh: a new send is a second question it will answer. Still pending when the person asks: say so with its `taskId`; unanswered, it ends `failed` after a day. A reply that does not answer the question is relayed as that; asking again is the person's call.

Report back: host, project and agent; the attributed answer, or the exact state or outcome.
