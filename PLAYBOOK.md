# Operator Playbook — from laptop to your first assignment fee

This is the hands-on guide. `CLAUDE.md` is the system's rulebook; this file is what you do, in order.
You work from a laptop and a phone. Nothing here needs a car.

**The plan in one paragraph.** Most of your first deals will come from **listing agents**, not homeowners. They
answer the phone, they have no Do-Not-Call problem, and the engine already has their phones and e-mails for every
stale or distressed listing it found. The hottest ones are **listed foreclosures**: houses with a sheriff's sale
date that are also on the MLS. The seller already wants to sell, the clock is running, and the agent earns nothing
if the house goes to auction. You call, you send a written offer the same day, and you follow up in 48 hours. Once
a contract is signed and attorney review ends, you sell the contract to a cash buyer for an assignment fee.

---

## Part 1 — one-time setup (about an hour)

### 1. Install and load the data
```bash
pip install -e ".[dev]"
python manage.py init
python -m swarm.orchestrator run-all      # scrape → enrich → stack → score → underwrite → guard → match (slow first time)
```
Then let it run every morning on its own: `python -m swarm.orchestrator daemon` (leave a terminal open), or run
`run-all` yourself each morning.

### 2. Put your business details in `config/settings.local.yaml`
This file is git-ignored, so it never leaves your laptop. Create it:
```yaml
operator:
  name: "Your Name"
  company_name: "Your Company LLC"         # the name on your LOIs, letters and e-mails
  phone: "9735550000"                       # a business line (a free Google Voice number works)
  email: "you@yourcompany.com"
  mailing_address: "PO Box 123, Edison, NJ 08817"   # required: e-mail footers (CAN-SPAM) and letter return address
contracts:
  title_company: "Agency name, address"     # once you've picked one (Part 1, step 5)
```
Until `name`, `phone` and `mailing_address` are set, letters print as watermarked PROOFS and SMTP e-mail is off.

### 3. Optional: send e-mail from the cockpit
Put a Gmail **app password** in `.env`. It needs 2-step verification on your Google account:
```
SMTP_USER=you@yourcompany.com
SMTP_APP_PASSWORD=xxxx xxxx xxxx xxxx
```
The engine sends at most 25 personalized e-mails in any 24 hours. Without SMTP, use the "Open e-mail" and "Copy"
buttons, send from your own inbox, then click **Mark sent**.

### 4. Do-Not-Call: register, download, load (before you call any homeowner)
1. Register as a telemarketer with the NJ Division of Consumer Affairs (N.J.S.A. 56:8-119 et seq.). It renews yearly.
2. Create an account at **telemarketing.donotcall.gov**. Your first five area codes are free.
3. `python manage.py dnc --status` lists the five area codes that cover most of your contacts. Download those.
4. `python manage.py dnc --load <downloaded file>`. Repeat every 31 days. The cockpit shows "stale" when it's due.

Listing agents' advertised business numbers are B2B calls, but if any agent says stop, you stop:
`python manage.py agent-blast --opt-out <contact id>`.

### 5. Pick your attorney and title company now, not on deal day
- `python manage.py partners --county Essex` lists NJ title agencies and closing attorneys we checked on their own
  websites. It shows phone numbers and quotes where a firm mentions investors, assignments or double closings.
- Call two of them. Ask: **"Do you insure an assignment of contract, and a same-day double close with separate
  funds?"** Put the one that says yes in `config/nj_closing_partners.yaml` under `preferred:`.
- Hire a NJ real estate attorney once to review the LOI, purchase agreement and assignment drafts in `deals/`
  (`python manage.py docs <id> --type all` makes a set).

### 6. Optional: your seller and buyer web pages
`python manage.py server` → http://127.0.0.1:8000/site/seller and /site/buyers. To host them free, run
`python manage.py site --build site_dist --endpoint <your Formspree form URL>` and upload `site_dist/` to GitHub
Pages or Netlify (see `site/README.md`). Import what comes in with `python manage.py inbound --ingest <export>`.

---

## Part 2 — every day: two focused hours

```bash
python manage.py today       # the checklist with today's real records
python manage.py server      # cockpit → http://127.0.0.1:8000 (the Today page is the same checklist, clickable)
```

**First:** clear anything under *Deadlines & follow-ups*. Closing deadlines come first, then 48-hour agent
follow-ups.

| Block | Time | What you do |
|---|---|---|
| 1 | 30 min | Call the 5 listed foreclosures. Fast Track tab → **Script** → call → **Packet + LOI** → send → **Mark sent** |
| 2 | 45 min | Work through the 15 ready-to-call fixers: call or e-mail the agent with the ask vs. MAO gap in front of you (Agent LOI Blast tab) |
| 3 | 25 min | Skip-trace 10 estates / vacant sheriff-sale houses: click the people-search links, paste results into the lead's Skip-trace box |
| 4 | 20 min | Call 5 cash buyers who bought in Elizabeth / Newark / Jersey City. Get their buy box and proof of funds *before* you have a deal |

Log every touch: in the cockpit (lead → **Log call**) or `python manage.py call <id> --outcome NO_ANSWER`.
Logging is what schedules your follow-ups, re-ranks leads and teaches the engine.

---

## Part 3 — your first 20 listing-agent calls

Start with `python manage.py today`, Block 1, then Block 2. For each one, open the script:
`python manage.py fast-track --script <id>` for listed foreclosures, or the cockpit lead page for the others.

**Opening (first 30 seconds — NJ rules):** your name, your company, why you're calling. *"Hi, this is ___ with
___. I'm a real estate investor, unrepresented, calling about your listing at ___."*

**For a listed foreclosure, say it plainly:** *"Public records show a sheriff's sale on ___ with an upset around
___. If it goes to the sale, your seller loses the house and the listing ends with it. I can put an as-is cash
contract in front of your seller today and close before the sale date."*

**Ask, don't pitch:**
- Is the seller living there, or is it vacant or rented? If it's the seller's home and in foreclosure, NJ's
  Foreclosure Rescue law applies. Record the answer: `python manage.py occupancy <id> VACANT --source "agent, 10/28 call"`.
  The engine re-prices on the spot.
- Do you know the payoff? Has the lender said anything about a short sale?
- What would the seller need to net to say yes?
- Can you send interior photos or do a quick video walk-through?

**Never say:** that you can stop or postpone the sale, that you'll negotiate with the lender (on a short sale,
that stays with the agent and the seller's attorney), or anything that hints you'll pay the agent or share their
commission. Don't call yourself a "cash buyer" if you'll assign. Say: *"I'm an investor; I buy with cash partners
and may assign the contract."*

**Voicemail:** name, company, the address, "I have a cash offer for your seller", your number. Text afterwards
only from your phone (the engine copies the text for you; it never sends texts itself).

What to expect: most agents will say no, or "send it over". The deals come from the written offer, the 48-hour
follow-up, and the next price cut. Every lead you mark sent comes back on your list two days later.

---

## Part 4 — when the agent says "send me the offer"

1. Cockpit → **Fast Track** (listed foreclosure) or **Agent LOI Blast** (everything else) → **Packet + LOI**.
   You get an e-mail with two options (an anchor, and a best price with an inspection period), a text message and a
   1-page LOI PDF.
2. Read it. Check the numbers against the lead page: ARV, repairs, comps. The engine never offers above the ask,
   and it never goes below the 82% FMV floor when the seller lives there.
3. Send it from your inbox (or **Send (SMTP)**), then click **Mark sent**. The lead moves to *offer sent to agent*
   with a follow-up call in 48 hours.
4. **If they ask for proof of funds:** be truthful. Tell them you'll be assigning to (or partnering with) a cash
   buyer, and offer that buyer's proof of funds or a letter from them. Never send someone else's bank statement as
   your own.
5. **Negotiating:** the anchor is where you start. The MAO at 70% of ARV is your walk-away. The 75% ceiling
   exists only for a strong buyer you've already lined up. If the seller needs more than your walk-away, thank the
   agent and ask to hear about any price change.

**If it's a FRFPA deal** (the seller's own home, in foreclosure): the purchase contract must be drafted by your NJ
attorney, in 14-point bold, notarized, with a 10-business-day cancellation right and at least 82% of fair market
value. The engine refuses to generate that contract. Send the LOI and the deal sheet to your attorney.

---

## Part 5 — signed contract → attorney review → assignment → paid

1. **Record the contract** on the day the fully signed contract reaches both sides:
   `python manage.py contract <id> --price 230000 --signed 2026-11-02`. This opens the closing file (8
   milestones, NJ business days, holidays skipped): `python manage.py closing <id>`.
2. **Attorney review — 3 NJ business days.** Either attorney can cancel for any reason. **Do not market the deal,
   invite buyers or collect deposits until it ends.** The engine blocks walkthroughs and bids until then. If an
   attorney disapproves: `python manage.py closing <id> --disapproved`.
3. **Order title** the next business day: `python manage.py closing <id> --title-email` drafts the e-mail to your
   title agency (address, block/lot, sellers, structure, lien and CO searches, the foreclosure payoff). Send it
   with the contract. Then `--done TITLE_SEARCH_AND_MUNICIPAL_LIEN_ORDERED`.
4. **Deposit:** wire the earnest money to the title company's escrow within 3 business days, then `--done EMD_DEPOSITED`.
5. **Sell the contract** (inspection period = 10 business days after attorney review):
   - `python manage.py match` and `python manage.py buyer-hotzones` show the buyers most likely to pay.
   - `python manage.py walkthrough <id> --invite <buyer>` gives you the access request for the agent or seller and
     the buyer invitation. Introduce the buyer truthfully: *"my buying partner, the investor I may assign the
     contract to."* Book everyone into one 90-minute window.
   - `python manage.py bid <id> --buyer <b> --amount 262k --emd 5k` for each written bid, then
     `python manage.py bid <id> --accept <bid id>`. That fills in the Assignment of Contract with the buyer's
     legal name and the fee (bid minus your contract price), and marks the other bids as backups.
6. **Clear to close → closing.** The title company closes. Your fee appears on the settlement statement. Then
   `python manage.py closing <id> --close --fee 32k` books it in your ledger.

---

## Part 6 — money and taxes

- `python manage.py finance` shows fees, expenses, net, your **28% tax reserve**, the probability-weighted
  pipeline, and the next quarterly estimated-tax date (IRS 1040-ES and NJ-1040-ES).
- Record every expense: `python manage.py finance --add-expense 120 --category mail --desc "50 letters"`.
- **The day a fee lands, move 28% into a separate savings account.** Assignment fees are self-employment income.
  Self-employment tax alone is about 14% of net profit, and federal and NJ income tax come on top. The 28% is a
  habit, not a calculation. Use the IRS / NJ worksheets or a CPA before you pay.

---

## Part 7 — weekly and monthly

- **Weekly:** run `python manage.py mail-merge --top 50` for owners you can't call. Review the letters, mail them
  (Click2Mail CSV included), then `--mark-mailed <batch dir>`. Run `python manage.py fast-track --portfolio-letters 20`
  to send one letter per landlord covering all of their addresses.
- **Every 31 days:** download fresh DNC data and load it.
- **Before every `git push`:** `python manage.py pii-scan` must print 0.
- **Read `logs/swarm_intelligence.md`.** It tells you which data is thin and which towns have the best spreads.

## The rules that keep you in business

1. You're an investor and principal, not an agent. Say you may assign the contract. Recommend an attorney.
   No pressure.
2. Never offer to stop, postpone or negotiate a foreclosure. Point people to the free help: HUD counseling
   1-800-569-4287, Legal Services of NJ 1-888-576-5529.
3. If the seller lives in the home and it's in foreclosure: FRFPA applies, your attorney drafts the contract,
   the seller gets at least 82% of FMV and 10 business days to cancel.
4. Nothing is marketed and no buyer walks the house until attorney review ends.
5. Call only 8am–9pm. Check every homeowner's number against Do-Not-Call. When anyone says stop, stop that day.
6. Never pay or accept a referral fee while you're unlicensed.
