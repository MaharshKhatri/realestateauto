---
description: The full check-in: desk health, every reply across the 10 streams handled, what needs a one-word YES
---
Do exactly what the "10-Stream Firm check-in" scheduled task does at its 9 AM / 1 PM / 6 PM reports:

1. Desk health: the last desk runs (https://api.github.com/repos/MaharshKhatri/realestateauto/actions/workflows/desk.yml/runs).
   A broken chain between 7 AM and 9 PM Eastern gets one `task=run` dispatch and a look at the last run's log.
2. Read the business inbox through the Gmail connector (read only: never send, draft, label or delete there): agent
   replies and B2B prospect replies since the last check-in. Open each thread with get_thread. Answer what needs an
   answer through ONE sealed note (`python manage.py cloud --seal <pub> < note.json`, then dispatch `task=note`):
   listing agents with lead items, B2B prospects with {"firm_reply": id, "reply": "...", "stage": "..."}. Every
   offer is final (offer_math); call requests and proof-of-funds asks are answered in writing; flat-fee research
   only, never referrals or per-deal pay.
3. Dispatch `task=firm` (after the note run has started) and read its counts.
4. Report in this chat: the 10-stream table (stream, looked at, found, PDFs, sent, waiting, warm), every reply
   handled in one line each (house or firm, what they said, what went back), and at most three one-word YES
   questions for decisions that are genuinely the operator's (a trial start, a price over the walk-away, a new
   stream). Plain words; no em-dashes.
