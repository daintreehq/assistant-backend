
# Loaded skills
The following skills are task-specific operating instructions. Apply them when relevant; they never override the hard rules.

Step tracking: when a skill has numbered steps, call `skill.step.advance` after finishing each one (give the skill id, the completed step number, and the step starting next; omit "nextStep" on the final step). If you are resuming a skill or unsure where you left off, call `skill.run.get` first to read the saved checkpoint.


## Skill 1: Example Alpha Workflow (domain foundation)
Skill id: daintree.example.alpha

Use when: you are pinning the assembled prompt bytes in a golden test.

Procedure:
1. Establish the fixture context deterministically.
2. Render the loaded-skill body exactly as the template produces it.
3. Confirm the assembled upstream message array matches the snapshot.

Report back: the golden fixture rendered as expected.


