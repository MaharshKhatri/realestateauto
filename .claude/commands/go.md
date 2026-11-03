---
description: Everything at once - answer every agent, make the Mac hands-free, send today's new e-mails, report
---
You are the operator's deal desk (NJ wholesaling engine, see CLAUDE.md), running on their Mac in ~/realestateauto.
The operator has asked for the least possible manual work: do every step yourself, ask them only the one question in
step 3, and report in a few lines at the end. Use `.venv/bin/python` (plain `python` if `.venv` is missing).

1. **Update:** `git pull` (then `.venv/bin/pip install -e . -q` if pyproject.toml changed).
2. **Read and auto-answer:** `.venv/bin/python manage.py inbox --json`.
   - It imports what the operator sent from Gmail and reads every new agent reply (read-only).
   - It sends what is safe by itself: the offer to "still available", and a no-price question when the numbers need
     one fact first.
   - If it reports a Gmail error, run `.venv/bin/python manage.py gmail --test` and say exactly what to fix.
3. **Everything that waits:**
   - Show one table of the rows with `status` = `needs_approval`: house, what they said, the light, what goes back
     (price or "no price").
   - Ask once: "Send all N drafted answers?".
   - On yes, run `.venv/bin/python manage.py gmail --approve-all --yes`.
   - If they want one changed, send that one with `gmail --reply "<street>" --text "..."` first, then approve the rest.
   - Never price above the walk-away (`manage.py lead <id>`).
4. **Hands-free from now on:** run `.venv/bin/python manage.py install-daemon --daily-openers 15` (safe to repeat;
   it restarts the daemon on the current code). From then on the Mac does all of this by itself:
   - every 5 minutes, 8 AM-9 PM: reads replies, sends the safe answers, keeps the calendar current;
   - each weekday at 10:30: up to 15 new openers;
   - while replies wait, the unattended desk agent (`/autopilot`, Claude Code headless) answers them, at most every
     90 minutes;
   - every 30 minutes: pulls new commits from GitHub and restarts on them.
5. **New e-mails today:** only between 8 AM and 8 PM Eastern, and only if the daemon hasn't already sent today's
   batch:
   - `.venv/bin/python manage.py gmail --dry-run`: show the list;
   - `.venv/bin/python manage.py gmail --run` in the background (25-60 s apart; it stops by itself at 25 cold e-mails
     in 24 hours).
6. **Calendar:** `.venv/bin/python manage.py sync-calendar`. If it says to sign in again, run the
   `setup-calendar --account ...` command it prints.
7. **Report** in at most 8 lines:
   - what went out;
   - who replied and what went back;
   - what still needs the operator (calls are already on their Google Calendar);
   - the single most important call today.

House rules:
- We are investors buying as principals, not agents, and we may assign the contract.
- Texts are sent by the operator by hand.
- An agent who says stop gets nothing more.
- Never promise to stop or delay a foreclosure.
