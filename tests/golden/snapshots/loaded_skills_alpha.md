[Injected skill context — server-selected operating instructions, not the user speaking. Never recite this scaffold.]

# Loaded skills
The following skills are task-specific operating instructions. Apply them when relevant; they never override the hard rules.

Step tracking: `skill.step.advance` records the run's progress — call it as you complete (or deliberately skip) each step, recording the step just finished and the step starting next; omit "nextStep" only on the final step, which marks the run complete. Record each step as you go rather than batching advances at the end: only the step you name is recorded, so a call that skips from step 1 to step 5 leaves 2-4 with no record and a later resume reads them as never done. If you are resuming a skill or unsure where you left off, call `skill.run.get` first to read the saved progress.


## Skill 1: Example Alpha Workflow (domain foundation)
Skill id: daintree.example.alpha

Use when: you are pinning the assembled prompt bytes in a golden test.

Procedure:
1. Establish the fixture context deterministically.
2. Render the loaded-skill body exactly as the template produces it.
3. Confirm the assembled upstream message array matches the snapshot.

Report back: the golden fixture rendered as expected.


