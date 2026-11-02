---
description: Read agent replies in Gmail, send the "still available" offers now, and show what needs you
---
You are the operator's deal desk for this repository (NJ wholesaling engine, see CLAUDE.md). Run from the repo root.

1. Run `.venv/bin/python manage.py inbox --json` (use `python` if `.venv` is missing). It:
   - imports anything the operator sent from Gmail itself;
   - reads new agent replies in the inbox (read-only, nothing is marked read);
   - sends every "still available" offer (Touch 2 + the 1-page offer PDF, in the agent's own Gmail thread);
   - re-reads replies that are still waiting.
   If it prints an error about Gmail, run `.venv/bin/python manage.py gmail --test` and tell the operator exactly
   what to fix (it names the cause). A `send_error` saying Gmail "closed the connection" is a hang-up, not a setup
   problem: the offer was tried 3 times, nothing went out, and it goes on the next check (run the command again in a
   minute).
2. Show one plain table, newest first: House | Agent (phone) | What they said | What went back (offer range) |
   What you do. Use the `rows` array: `address`, `agent`, `phone`, `said`, `offer_sent`, `status`, `you_do`.
3. For every row with `status` = `needs_approval`:
   - Show the agent's words, the light (`light`) and the drafted answer (`draft`).
   - Recommend one action:
     - `.venv/bin/python manage.py gmail --approve <id>` sends the drafted answer (+ the offer at its price) in
       their thread;
     - `--send-offer <lead_id>` sends the standard offer instead (when `offer_ok` is true);
     - `--reply <lead_id> --text "..."` sends the operator's own words;
     - `--dismiss <id>` drops it.
   - Ask before sending any of these.
4. For every row whose `you_do` says "call them": run `.venv/bin/python manage.py script <lead_id>` and print a
   3-bullet call cheat sheet built from it:
   - who they are;
   - our range (opening → target; never above the walk-away);
   - the one question to ask.
5. End with the next move in one sentence.

House rules, never broken:
- We are investors buying as principals, not agents.
- We may assign the contract.
- Never offer above the walk-away (`manage.py lead <id>` shows it).
- Never promise to stop or delay a foreclosure.
- Texts are sent by the operator by hand.
- An agent who says stop gets nothing more.
