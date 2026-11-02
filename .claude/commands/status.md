---
description: Pipeline, today's sends, warm agent threads and the next move
---
Run from the repo root.

1. `.venv/bin/python manage.py gmail --status`: today's counts, replies waiting on the operator, queued offers.
2. `.venv/bin/python manage.py inbox --no-send --json`: the reply table, nothing sent.
3. `.venv/bin/python manage.py stats`: the pipeline.

Then print, in plain English, four short sections:
- **Today:**
  - cold e-mails sent (out of 25);
  - replies received;
  - offers out.
- **Warm threads:**
  - one line per agent who wrote back;
  - columns: house, what they said, what we sent, what's next.
- **Waiting on you:** anything in `needs_approval`, with the exact command to clear it.
- **Next move:** one sentence.
