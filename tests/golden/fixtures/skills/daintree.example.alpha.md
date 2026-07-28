---
id: daintree.example.alpha
title: Example Alpha Workflow
summary: A representative fixture skill used to pin golden prompt rendering.
whenToUse: Use for the golden fixture workflow when exercising prompt assembly in tests.
foundation: true
tags:
  - fixture
  - golden
risk: read
requiredTools:
  - context.snapshot
  - terminal.read
---
Use when: you are pinning the assembled prompt bytes in a golden test.

Procedure:
1. Establish the fixture context deterministically.
2. Render the loaded-skill body exactly as the template produces it.
3. Confirm the assembled upstream message array matches the snapshot.

Report back: the golden fixture rendered as expected.
