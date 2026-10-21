---
id: daintree.hosts.copy-portable-settings
title: Copy this machine's portable settings to a Remote Host
summary: 'Guide the person through Settings → Hosts → a connected host → Copy settings: see what a copy would change, choose categories, preview, copy once and check the applied and skipped counts, or undo the last copy, with credentials, consent, trust and plugins never travelling.'
whenToUse: 'Use when the person wants to use COPY SETTINGS: put this machine''s Daintree settings (agent flags and presets, notification preferences, recipes, theme) on a Remote Host that is already connected, see which categories a copy would change there before anything is written, check what the last copy applied or skipped, or take the last copy back while keeping edits made on the host since. It is the person''s own one-shot push in Settings → Hosts, never host to host and never syncing afterwards. The deliverable is a preview first, then only the categories the person chose copied, the applied and skipped counts checked on the host, or the last copy undone leaf by leaf; credentials, consent and trust, project and run state and plugin installs never travel. Do not use for adding the machine (daintree.hosts.connect-host), durable instructions and project memory (daintree.memory.manage-project-memory), a plugin the person is writing (daintree.plugins.develop-project-plugin), why the same code behaves differently on two hosts (daintree.hosts.diagnose-host-differences), code changes a run made on the hosts (daintree.hosts.undo-swarm-changes), or a read-only report of how the hosts are doing (daintree.hosts.survey-hosts).'
risk: system
hostEligibility: configured
---
The person does every step in Settings → Hosts → the host → Copy settings and reads you what it shows: no action can. It pushes THIS machine's settings to that host, once; never host to this machine or host to host. No tool starts it, here or through `host.call`.

1. The host must be Connected ("Connect to … to copy settings": daintree.hosts.connect-host) and new enough ("too old to receive settings": updating it there is that runbook's, the person's call).
2. "Compared with <host>" writes nothing: how many categories a copy would change. "Too many settings … to compare": preview fewer categories. A request to compare ends at step 4 and authorises no copy.
3. Ticked by default: agent, notification, worktree, recipe, hibernation, session, idle and update categories. Off: Global environment variables, Permission bypass (skips agents' prompts), Theme and keyboard shortcuts: tick one only for what the person named, never Select all.
4. "Preview changes on <host>": per category Replaced, Added or Skipped (reason); "Stays on this computer" lists what looks like a secret. Summarise by label; values of environment variables, flags and paths stay on their screen. Changing a tick drops the preview.
5. Only on the person's go: "Copy to <host>". Cancel changes nothing.

Changed since the preview, or the link dropped: Refresh, preview and review again, a new go; never click on the old one. "Nothing changed" is a clean rollback; "may be partly changed" is read from a new preview.

6. Verify: the result's applied, unchanged, skipped and failed counts against the preview; after Refresh "Compared with" no longer listing what was copied, and "Last copied from <name> <time>" with the same applied and skipped. The name is attribution.

Undo: "Undo last copy" on that row, the latest copy only, absent or "Can't be undone" when no record is kept. The dialog lists what goes back; anything edited on the host since stays. A newer copy replaces it. After it, or a dropped link, Refresh: a "restored" count, "Kept · Changed … since the copy" edits, "Couldn't restore" leaves still as copied. Never repeat an undo blindly.

Credentials, consent (assistant control, "Let other machines' assistants work here"), trust, project and run state and plugin installs never travel. Plugins: the host's Plugins page, one at a time; trust is per machine.

Report back: host, categories ticked, the preview's changes and skips, the person's approval, the counts, restored count, kept and failed leaves.
