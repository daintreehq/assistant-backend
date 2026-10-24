---
id: daintree.hosts.configure-settings
title: Read or change a named setting on Remote Hosts
summary: 'From this Shell, read a named Daintree setting on Remote Hosts, or change it on the hosts named (Canopy on across several among them): preview the exact change per host, apply it on the person''s one approval of that preview, and report what each host took, refused or could not be reached, with the values it reported.'
whenToUse: 'Use when the person wants one of the HOST SETTINGS Daintree can change from here read or changed on their other machines: turn Canopy on, off or hidden on all their hosts or the ones named; which agent a host starts by default or runs its assistant with; a host''s alert about idle terminals and its minutes; what a host has set for one of these now; or change just that setting and say which hosts took it. The deliverable is the hosts and setting resolved, then either a read that writes nothing, or one preview the person approves once for the listed hosts, applied once, and per host what changed, was already set, was refused or could not be reached, with the values the host itself reported. Do not use for copying this machine''s setting categories to a host (daintree.hosts.copy-portable-settings), a read-only report of the hosts'' connection, load and agents (daintree.hosts.survey-hosts), why a host refuses calls and which gate the person changes (daintree.hosts.survey-hosts), adding a machine or one that will not connect (daintree.hosts.connect-host), or putting this project on a host (daintree.hosts.open-project-on-host).'
risk: system
requiredTools:
  - host.list
  - host.settings
hostEligibility: configured
---
`host.settings` reads and changes five settings on hosts by host id, called directly (never through `host.call`) and in no project; its outcomes below replace the foundation's `host.call` rules. It never touches this machine: that is the person's own Settings.

1. Resolve. `host.list`: each named host to its `hostId`; "all hosts" is every listed host except `local`, resolved once: name which are connected and which are not. Map the request to keys: `canopyMode` (`on`, `unset` = off, `hidden`), `agentDefaults.defaultAgent`, `agentDefaults.assistantAgentId` (`null` = not set, the host's own default), `idleTerminalNotify.enabled`, `idleTerminalNotify.thresholdMinutes` (15 to 1440). Any other setting, secret, path or store has no remote control: say so; never ssh, edit a file or copy settings to reach it. Canopy's filter of which hosts' cards it shows is not `canopyMode`: never change a host to change a view.
2. A request to read (or read, then change): read the named keys, up to 8 hosts and 5 keys. A read-only request ends here.

```json tool=host.settings
{"operation":"read","hostIds":["<hostId>","<hostId>"],"keys":["canopyMode"]}
```

3. Preview: one patch for the hosts named, up to 8. Over 8: say so; for Canopy the person's own "Enable Canopy on all hosts…" in Canopy's host menu takes them all in one dialog; else batches, each its own approval, only if the person agrees.

```json tool=host.settings
{"operation":"preview","hostIds":["<hostId>","<hostId>"],"patch":{"canopyMode":"on"}}
```

   Show each host's `value` → `proposed`. No `ticket` when a host could not be read (its row says why: preview again without it, and name it) or nothing would change (report it already set). Top-level `policy_rejected`: the value is refused.
4. Apply the `ticket` once, within 5 minutes, only for a change the person asked for:

```json tool=host.settings
{"operation":"apply","ticket":"<ticket>"}
```

   The app asks the person once, in this assistant's window, listing each host and change and, where Canopy turns on, that the host sends its agents' terminal output to Daintree's servers. That dialog is the only approval: never the request, a pairing or an argument of yours. The app holds the revisions and the host's launch in the ticket and refuses a write against any other. Read the top level first; nothing was written when it is: `expired` (spent by its first apply, old, or another assistant's), `opted_out` (control off for a host: the whole apply stopped), `policy_rejected` "declined" (stop: a change only on a new request) or "couldn't be asked" (the person opens the assistant's window, then a new preview).
5. Per host: `ok` changed, or already set; `conflict`: it changed there or restarted since the preview, but "Canopy was shown there, but not turned on" means the rest landed; `policy_rejected` (the host refused), `disconnected`, `unsupported` (old Daintree, or an answer it could not read), `rate_limited` (wait, then preview again); `ambiguous`: may have landed. Quote each `detail`.
6. Verify: read the named keys on every host the apply reached. The values are each host's own.

A lost answer to an apply is never retried: read first; `expired` does not mean nothing was written. Never repeat a change on a host that took it; a retry is a new preview of what is still unchanged, and a new approval. Hosts connected after the preview are outside it. Turning a setting back is a new request.

Report back: per host, name and id, values before (from the read or preview), outcome and detail, values read after; hosts left out and why; approved, declined or not asked. Revisions are held by the app, not shown: never report one.
