---
description: Run all unit tests (wa-service Jest + processor/bot/analytics pytest)
model: haiku
---

# Test Skill

Run all tests across all services and report results.

## Steps

1. Run wa-service Jest tests:
   ```
   cd wa-service && npx jest --forceExit 2>&1
   ```

2. Run processor pytest:
   ```
   cd processor && python3 -m pytest tests/ -v 2>&1
   ```

3. Run bot pytest:
   ```
   cd bot && python3 -m pytest tests/ -v 2>&1
   ```

4. Run analytics pytest (Jev tests skip themselves when `typesafe-sdk` is not installed):
   ```
   cd analytics && python3 -m pytest tests/ -v 2>&1
   ```

5. Report summary:
   - Total passed / failed per service
   - If any failures: show the failing test names and error messages
   - If all pass: confirm "All tests passed ✓"
