---
description: The daily run - answer replies first, refresh the data, send today's paced openers, sync the calendar
---
Run from the repo root (`.venv/bin/python`, or `python` if `.venv` is missing). Do these in order and report each step
in one line.

1. **Replies first:** run the /inbox steps (`manage.py inbox --json`). A warm agent beats a cold one.
2. **Fresh deals:**
   - Run `.venv/bin/python -m swarm.orchestrator run-all` in the background (10-30 minutes: sheriff sales,
     listings, contacts, underwriting, pre-flight).
   - Wait for it, then show the last lines of its output.
   - If it fails, say which step failed and continue with yesterday's data.
3. **Preview the batch:**
   - Run `.venv/bin/python manage.py gmail --dry-run`.
   - Show the list: house, kind, touch, agent.
   - Show the skipped ones and why.
4. **Send the batch:**
   - Only between 8 AM and 8 PM Eastern.
   - Run `.venv/bin/python manage.py gmail --run` in the background.
   - It sends the 2-line openers 25-60 seconds apart and stops by itself at 25 cold e-mails in 24 hours (replies
     to agents who wrote back are never counted against that).
   - When it ends, show how many went out.
5. **Calendar:** run `.venv/bin/python manage.py sync-calendar`.
6. **Report:**
   - what went out;
   - who is waiting on the operator;
   - the single most important call to make today (the hottest reply: backup offer, "call me", a counter).

Never send outside 8 AM-8 PM, never more than the cap, never to an agent who asked to stop.
