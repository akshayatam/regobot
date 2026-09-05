# Regodit Agent Instructions

Before making project changes:

1. Read `BASE_MODEL_PROMPT.md`.
2. Read the phase file explicitly named by the user.
3. Execute only that phase.
4. Preserve working behavior from previous phases.
5. Never modify source files under `data/`.
6. Never fabricate company evidence.
7. Run and test code before claiming completion.
8. Do not advance to another phase without explicit instruction.

Priority order:

1. User's current instruction
2. BASE_MODEL_PROMPT.md
3. Current phase prompt
4. Existing project conventions

At the end of each phase report:
- files changed
- commands
- tests
- acceptance criteria
- limitations
