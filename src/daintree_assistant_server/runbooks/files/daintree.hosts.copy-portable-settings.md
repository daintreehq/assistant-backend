---
id: daintree.hosts.copy-portable-settings
title: Copy this machine's portable settings to a Remote Host
summary: 'Guide the person through Settings → Hosts → a connected host → Copy settings: see what a copy would change, choose categories, preview, copy once and check the applied and skipped counts, or undo the last copy, with credentials, consent, trust and plugins never travelling.'
whenToUse: 'Use when the person wants to use COPY SETTINGS: put this machine''s Daintree settings (agent flags and presets, notification preferences, recipes, theme) on a Remote Host that is already connected, see which categories a copy would change there before anything is written, check what the last copy applied or skipped, or take the last copy back while keeping edits made on the host since. It is the person''s own one-shot push in Settings → Hosts, never host to host and never syncing afterwards. The deliverable is only the categories the person chose copied after a preview, the applied and skipped counts checked on the host, or the last copy undone leaf by leaf; credentials, consent and trust, project and run state and plugin installs never travel. Do not use for adding the machine (daintree.hosts.connect-host), durable instructions and project memory (daintree.memory.manage-project-memory), a plugin the person is writing (daintree.plugins.develop-project-plugin), why the same code behaves differently on two hosts (daintree.hosts.diagnose-host-differences), code changes a run made on the hosts (daintree.hosts.undo-swarm-changes), or a read-only report of how the hosts are doing (daintree.hosts.survey-hosts).'
risk: system
---
The person does every step in Settings → Hosts → the host → Copy settings and reads you what it shows: no action can. It pushes THIS machine's settings to that host, once; never host to this machine or host to host. A host's assistant cannot start it.

1. The host must be Connected ("Connect to … to copy settings": daintree.hosts.connect-host) and new enough ("too old to receive settings": update it there).
2. "Compared with <host>" writes nothing: how many categories a copy would change, read on opening and on Refresh. "Too many settings to compare": copy fewer categories. A request to compare ends at step 4 and authorises no copy.
3. Ticked by default: agents, models, notifications, worktree path, recipes, hibernation, session restore, idle terminals, update checks. Off: Global environment variables, Permission bypass (skips agents' prompts there), Theme and keyboard shortcuts: tick one only for what the person named, never Select all.
4. "Preview changes on <host>": per category Replaced, Added or Skipped (reason); "Stays on this computer" lists what looks like a secret. Summarise by label; values of environment variables, flags and paths stay on their screen. Changing a tick drops the preview.
5. Only on the person's go: "Copy to <host>". Cancel changes nothing. One click per preview.

Changed since the preview, or the link dropped: Refresh, preview and review again, a new go; never click the old one again. "Nothing changed" is a clean rollback; "may be partly changed" is read from a new preview.

6. Verify after Refresh: each category's applied, unchanged, skipped and failed counts against the preview, "Compared with" no longer listing what was copied, and "Last copied from <name> <time>" with the same totals. The name is attribution, not proof.

Undo: "Undo last copy" on that row, the latest copy only, absent when it says "Can't be undone". The dialog lists what goes back; anything edited on the host since stays ("Kept · Changed on … since the copy"). A newer copy replaces it. Verify "settings restored" and the kept list.

Credentials, consent, trust, project and run state, terminal settings and plugin installs never travel. Plugins: the host's Plugins page, one at a time; trust is per machine.

Report back: host, categories ticked, the preview's changes and skips, what the person approved, the counts, restored and kept leaves; the screen versus the person's word.
