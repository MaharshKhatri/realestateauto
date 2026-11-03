---
description: Reply to an agent in plain English - "/reply 22 Grand Ave tell him I'll call at 5 and that 400k is my number"
argument-hint: <house or agent> <what to say>
---
The operator wants to answer a listing agent: `$ARGUMENTS`

1. Find the lead: `.venv/bin/python manage.py gmail --find "<the house or agent name from the request>"`.
   If several match, pick the one with `waiting_reply` set or ask which one.
2. Read the numbers: `.venv/bin/python manage.py lead <lead_id>`. Note the opening offer, target and walk-away.
   If the operator asks for a price above the walk-away, don't send it: show the walk-away and ask.
3. Write the e-mail body in the house style (CLAUDE.md, Phase 5 "Human copy"):
   - 3-5 short plain sentences, under 110 words, first name greeting;
   - no em-dashes, no markdown, no bullets;
   - "I'm an investor buying for myself or a partner investor" when who we are comes up;
   - never "cash buyer", never "off-market" for a listed house, never a promise about a foreclosure.
   Don't add the signature or footer: the command appends the signature, mailing address and opt-out.
4. Save it to a temp file and send it in the agent's Gmail thread:
   `.venv/bin/python manage.py gmail --reply <lead_id or "street"> --body-file <file>`
   - Add `--price <N>` when the message states a price: that attaches the 1-page offer at N and logs it as an offer.
5. Print exactly what was sent (to, subject, body) and the follow-up it scheduled.
