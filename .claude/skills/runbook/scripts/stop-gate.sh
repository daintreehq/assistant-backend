#!/usr/bin/env bash
# Stop hook for /runbook. The run may end only when the LAST non-empty line of the
# last assistant message is exactly one of RUNBOOK_COMPLETE, RUNBOOK_BLOCKED or
# CATASTROPHIC_STOP (bold or code formatting around it is tolerated); a sentinel merely
# mentioned in a sentence does not count, and neither does one in an earlier message.
# Anything else blocks the stop and tells the model where to pick up.
set -u

input="$(cat)"
transcript="$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null)"

# The text of the last assistant record that said or did anything (thinking-only
# records are skipped). A last record that is a bare tool call yields "" and blocks.
last_message() {
  [ -n "$transcript" ] && [ -f "$transcript" ] || return 0
  tail -n 200 "$transcript" 2>/dev/null \
    | jq -c 'select(.type=="assistant" and ((.isSidechain // false) | not))
             | {text: ([.message.content[]? | objects | select(.type=="text") | .text] | join("\n")),
                tool: ([.message.content[]? | objects | select(.type=="tool_use")] | length > 0)}
             | select(.text != "" or .tool)' 2>/dev/null \
    | tail -n 1 | jq -r '.text' 2>/dev/null
}

sentinel() {
  printf '%s\n' "$1" | awk 'NF { line = $0 } END { gsub(/^[ \t*`]+|[ \t*`]+$/, "", line); print line }'
}

# The transcript can lag the final message by a moment.
last=""
for _ in 1 2 3 4 5; do
  last="$(sentinel "$(last_message)")"
  case "$last" in RUNBOOK_COMPLETE|RUNBOOK_BLOCKED|CATASTROPHIC_STOP) break ;; esac
  sleep 0.3
done

case "$last" in
  CATASTROPHIC_STOP)
    printf '%s\n' '{"continue":false,"stopReason":"Catastrophic failure: /runbook halted."}'
    exit 0 ;;
  RUNBOOK_COMPLETE|RUNBOOK_BLOCKED) exit 0 ;;
esac

jq -cn --arg reason "RUNBOOK PIPELINE INCOMPLETE: no outcome sentinel in your last message. Scan back for your most recent position line (Phase N/11 <name> verified. Next: M/11 <next>.), read that row of the Pipeline table in .claude/skills/runbook/SKILL.md, and continue it now, in this turn. Waiting on a background Codex call, checks agent or trial: wait for its completion notification, or read it by the exact id its launch returned; never guess an id. Recovery: REQUIRES_REBASE or a rejected push: rebase onto origin/develop, rerun the Pre-PR gates, push again (max 2 cycles). CI_FAIL with real failures: fix, verify through a Haiku checks agent, commit, push, re-invoke /work:ci (Fix-caps). CI_FAIL listing only pending checks: re-invoke /work:ci. MERGE_CONFLICT: rebase, recompile the corpus if its generated files conflicted, rerun the gates, push with --force-with-lease, re-invoke /work:ci. A dependency or authorisation you do not have, or a required gate that cannot be made green: emit the Blocked report, its last line exactly RUNBOOK_BLOCKED. Finished: emit the Final report, its last line exactly RUNBOOK_COMPLETE (re-emit it whole if you already wrote it and were still blocked). Only for unrecoverable failures (auth, repo corruption, filesystem) end with CATASTROPHIC_STOP." \
  '{decision:"block", reason:$reason}'
exit 0
