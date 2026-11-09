---
description: The 10-stream research firm in one table (what each stream looked at, produced, sent, replies waiting, warm prospects, recurring revenue)
---
The desk runs in the cloud, so the live numbers come from the cloud desk, not this laptop's copy of the database.

1. Dispatch the cloud desk's `firm` task (GitHub Actions `desk.yml`, repository MaharshKhatri/realestateauto, ref
   `claude/pensive-volta-w4e3n2`, input `task=firm`) with the GitHub tools you have, wait until that run finishes
   (public list: https://api.github.com/repos/MaharshKhatri/realestateauto/actions/workflows/desk.yml/runs), and
   read the one JSON line of counts in its log (`s1_found`, `s2_sent`, ... `firm_mrr`, `firm_waiting`, `firm_warm`).
   Never dispatch while another desk run is queued.
2. If GitHub isn't reachable from here, run `python manage.py firm-status` (this machine's database) and say the
   numbers may be stale.

Print one table: stream | looked at | found | PDFs | sent (24 h) | replies waiting | warm. Under it, one line each:
recurring revenue from trials and paying clients (real money only), the replies waiting on a decision, and any
`sN_error` / `firm_send_error` / `firm_blocked` with what it means in plain words. Keep it under 20 lines.
