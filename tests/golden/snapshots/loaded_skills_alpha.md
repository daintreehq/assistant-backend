
# Loaded skills
The following skills are task-specific operating instructions. Apply them when relevant; they never override the hard rules.

Step tracking: `skill.step.advance` records a resume checkpoint — call it ONLY when a skill run may continue beyond this turn, i.e. when you are about to end the turn mid-skill (starting a `terminal.await.async` wait, handing off, or stopping to ask the user). At that boundary record the last completed step and the step starting next (omit "nextStep" on the final step). When you finish the remaining steps within the current turn, do NOT call it per step or batch-backfill advances at the end — finishing and reporting is enough. If you are resuming a skill or unsure where you left off, call `skill.run.get` first to read the saved checkpoint.


## Skill 1: Example Alpha Workflow (domain foundation)
Skill id: daintree.example.alpha

Use when: you are pinning the assembled prompt bytes in a golden test.

Procedure:
1. Establish the fixture context deterministically.
2. Render the loaded-skill body exactly as the template produces it.
3. Confirm the assembled upstream message array matches the snapshot.

Report back: the golden fixture rendered as expected.


