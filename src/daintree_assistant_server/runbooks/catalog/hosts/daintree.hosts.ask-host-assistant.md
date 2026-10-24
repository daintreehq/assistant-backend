---
id: daintree.hosts.ask-host-assistant
title: Ask a remote host's assistant one question
summary: Put one question to the Daintree assistant on one Remote Host, wait briefly without resending, and relay its answer attributed to that machine, caveats kept and apart from anything checked here.
whenToUse: 'Use when the person wants ONE QUESTION answered by the Daintree assistant on ANOTHER MACHINE, a Remote Host this Shell reaches: "ask the build box how the release prep is going", "what does the assistant on my other Mac make of the failing build", "check with the laptop host whether the migration finished and tell me what it says". The deliverable is that assistant''s ANSWER, relayed with attribution and its caveats; the question asks it to change nothing. Do not use for a question to an agent on THIS machine (daintree.agents.ask-running-agent for one already open, daintree.agents.ask-and-relay to launch one), to answer a question an agent here is asking (daintree.agents.answer-waiting-agent), for work that machine''s assistant should carry out (daintree.hosts.delegate-to-host), for how controlling another machine works (daintree.hosts.foundation), or for why it cannot be reached or refuses (daintree.hosts.survey-hosts).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
One question to one host; the deliverable is its answer, attributed. Nothing asked here changes anything.

Procedure:
1. `host.list`: the named host (not `connected`, or `control:false`: report it, stop) and, in its `projects`, the one the question is about. Several hosts or projects fit: ask which.
2. A fact about that host's state (what runs there, whether an agent finished, which branch or commit a worktree is on, a check's result): read it yourself through `host.call`, no assistant needed: `terminal.getStatus` with `includeOutput:{"lines":50}`, `worktree.list`, a read-only `project.runCommand` (`git log -1`). Relay it as read on that host, with the tool and time, and stop.
3. Its assistant's judgment or context ("what does it make of", "how is it going" on work it runs): `assistant.list` through `host.call` there. Several: the one named, else ask. None: say so; a new one has none of that context, so `assistant.start` only if the person wants a fresh view. Not at its prompt (`agentState:"waiting"`, `waitingReason:"prompt"`): working, arm `terminal.notifyWhenIdle` on its `terminalId` through `host.call` and end the turn; stopped on a question or error, report that and stop.
4. Ask once, its `terminalId` and `generation` from the listing:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"terminal.sendCommandOwned","args":{"terminalId":"<assistant terminalId>","generation":3,"command":"Question from the person here: <question verbatim>. Question only, not work: answer read-only; change no files or state, start no other work, and any agent you use only reads. At most 30 lines; say what you are unsure of.","handback":true,"waitForReply":true,"waitSeconds":180}}
```

5. By `reply`:
   - `waitingReason` `question` or `approval`: stopped on a prompt in its own pane, which only a person there answers: report it.
   - `outcome` `handback` or `settled`: the answer is `reply.handback`, else `reply.reply.text` (cut: `terminal.readLastMessageOwned` there). A reply asking you something: answer once, only from what the person already said; permission to act is never in scope, so ask the person. Else step 6.
   - `timeout`: still answering. Arm `terminal.notifyWhenIdle` on it, tell the person it is with that host, end the turn; never resend.
   - `exited`, `closed`, or no answer text: report that, with no guessed cause. `ASSISTANT_BUSY`: step 3's not-at-prompt case; `ASSISTANT_RESTARTED`: list again, then step 4.
6. Relay it as that machine's words: "<host name>'s assistant (<agentId>, <project>) says:", quoted or condensed faithfully, every caveat and doubt kept, apart from anything you read yourself. It says it changed something: report that as a breach of the question's boundary.

On notice or resume: read `terminal.getStatus` there first. Armed before asking: back at its prompt, list again and step 4. Armed after asking: read the answer with `terminal.readLastMessageOwned` through `host.call`, then step 6. Already relayed: nothing more.

Stop and recovery: one question per request, never sent afresh: a new send is a second question it will answer. One armed notice at a time. A reply that does not answer the question is relayed as that; asking again is the person's call.

Report back: host and project; what answered (the assistant's `agentId` and `terminalId`, or the tools you read); the attributed answer, or the exact state or outcome.
