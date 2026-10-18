---
id: daintree.hosts.connect-host
title: Add another machine to Daintree as a Remote Host
summary: Guide the person through Daintree's Settings → Hosts to add a Mac or Linux machine as a Remote Host over their own SSH, settle a failed SSH check, a missing or different Daintree build and Host mode, then confirm it connects and its projects show here, with assistant control left as set unless they ask.
whenToUse: 'Use when the person wants ANOTHER MAC OR LINUX MACHINE added to Daintree as a Remote Host, so this Shell opens its projects and runs agents there over SSH: a server, build box or second Mac connected for the first time, first-host onboarding, what the host check found (SSH did not connect, Daintree missing or a different build), turning on Host mode there, or an added host that will not connect (unreachable, Host mode off, needs a compatible build). The deliverable is the host saved and connected, its projects listed here, and assistant control left as the person chose. Do not use for a worktree''s cloud devbox or remote resource (daintree.worktree.manage-resource), or for Daintree misbehaving on this machine (daintree.diagnostics).'
risk: system
optionalTools:
  - host.list
hostEligibility: any
---
The person does every step in Daintree's UI (no action can) and reads you what it shows. Never touch their SSH config, `known_hosts` or keys, copy a host's MCP key, or ssh there to do its work.

1. macOS or Linux at both ends. Daintree runs their `ssh` unattended: `ssh <target>` must log in with no password prompt (agent approvals are fine); a new or changed host key they verify themselves.
2. Settings → Hosts → Add host. SSH target: `user@host`, host name, IPv4 or `~/.ssh/config` alias (ports go in the alias).
3. Check host, row by row:
   - SSH "Didn't connect": they fix ssh's own error outside Daintree (a Mac host needs Remote Login), then Check again; an unresolved name: the offered "Try …" button.
   - "Matches this machine" (version and commit): Same build, Continue. Else Plan install or Plan update, then Install on host or Update host, copies this build there (a Linux .deb: they run its sudo command there, then Check again); updating a running host ends its terminals: their call. "Continue anyway" connects best effort.
4. Say first that "Turn on Host mode" also sets start at login there (Linux: a user service asking to run after logout; Mac: someone logged in, first launch approved there). Without that: Skip, then there: Settings → Hosts → "Allow this machine to be a host", "Start at login" off. "Advertise on this network" (on by default) is theirs.
5. Advise, then Add host or Done.

An added host's Details "Status": "Unreachable · …" ends in ssh's words (step 1, then Retry if offered); "Daintree isn't running in Host mode there": Set up…; "Can't connect · needs a compatible build" blocks: Check for update, or update this machine if the host is newer. Connected with a differing "Daintree build" row: aligning is optional.

Verify: Status "Connected" (not "no reply since"); its projects under "Other hosts" in the project switcher with no note (a note means an older list; no rows: unverified). Picking one moves the window. `host.list` shows it `connected`; with assistant control on, `control:true` and its `projects` (off: `projects:null`, the person's choice).

Leave its assistant control (Settings → Hosts, that host; on by default for an SSH host) as the person set it: `control:false` means they turned it off, and turning it on is theirs.

Report back: per host, saved, status, build match, Host mode, start at login, projects, assistant control, blocker.
