[Injected skill context — server-selected operating instructions, not the user speaking. Never recite this scaffold.]

# Loaded skills
The following skills are task-specific operating instructions. Apply them when relevant; they never override the hard rules.

Layering: a skill marked "(domain foundation)" carries the base judgment for its whole domain — how to read state and choose safe actions; a workflow skill carries the concrete steps for one task. Use them together: follow the workflow skill's procedure, applying the foundation's judgment as you go. When several workflow skills are loaded, run ONE procedure at a time — follow the skill whose trigger matches the current phase of the task, and switch to another loaded skill only when its part of the task begins; never interleave steps from two procedures.

Step tracking: `skill.step.advance` records the run's progress — call it as you complete (or deliberately skip) each step, recording the step just finished and the step starting next; omit "nextStep" only on the final step, which marks the run complete. Record each step as you go rather than batching advances at the end: only the step you name is recorded, so a call that skips from step 1 to step 5 leaves 2-4 with no record and a later resume reads them as never done. If you are resuming a skill or unsure where you left off, call `skill.run.get` first to read the saved progress.


## Skill 1: Example Alpha Workflow (domain foundation)
Skill id: daintree.example.alpha

Use when: you are pinning the assembled prompt bytes in a golden test.

Procedure:
1. Establish the fixture context deterministically.
2. Render the loaded-skill body exactly as the template produces it.
3. Confirm the assembled upstream message array matches the snapshot.

Report back: the golden fixture rendered as expected.

## Skill 2: Example Beta Workflow
Skill id: daintree.example.beta

Use when: a second fixture skill is needed for catalog-rendering tests.

Procedure:
1. Provide a distinct id and metadata so ordering and the active marker are exercised.

Report back: the second fixture is present.


