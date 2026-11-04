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

### 1. Install once, then use the Desktop icon
```bash
pip install -e ".[dev]"
python manage.py init
python manage.py install-shortcut          # puts "Launch NJ Wholesale Dashboard" on your Desktop
```
From now on you never need the terminal: **double-click the Desktop icon**. It starts the dashboard (if it isn't
already running), waits until it answers, and opens http://127.0.0.1:8000. The same launchers are in the project
folder: `Launch_Dashboard.command` (Mac), `launch_dashboard.sh` (Linux), `Launch_Dashboard.bat` (Windows). If it
ever doesn't start, `logs/server.log` says why.

First data load: click **🤖 Run Autopilot Data Refresh → Full refresh** (10–30 minutes the first time). After that,
a Full refresh each morning (or leave `python -m swarm.orchestrator daemon` running) keeps it current.

**Already ran the swarm in a cloud session? Bring that database over instead of re-scraping.** In the cloud session
`python manage.py db-sync --pack` writes an encrypted copy (`wholesaling.db.xz.enc` + `manifest.json`) to
`data/snapshots/`. Download both files and put them in `data/snapshots/` on the laptop (they are git-ignored: this
repository is public). The Desktop icon then notices there is no database yet and asks once for the passphrase
(`DB_SYNC_KEY`); or run `python manage.py db-sync --unpack 'PASSPHRASE'` yourself. The passphrase is then saved in
your `.env`, so it never asks again. For a newer snapshot later, replace the two files and run
`python manage.py db-sync --unpack --force` (your current copy goes to `data/backups/` first). Treat the passphrase
like a bank password: anyone with it and the file can read every name in the database.

### 2. ⚙️ My Setup & Auto-Email (2 minutes)
Click **⚙️ My Setup & Auto-Email** in the top bar and fill in: your full name, company name, Google Voice number,
e-mail, and mailing address (a PO box is fine). Everything you send signs with these, and the law needs all five
(CAN-SPAM postal address on every e-mail, a callback number on every call). Saving writes
`config/settings.local.yaml` and `.env` on this laptop only (both git-ignored). The bar then shows
**✅ Setup Complete — Live Mode Active**, and letters stop printing as watermarked PROOFS.

**Optional, sending from the dashboard:** turn on 2-step verification for your Google account, create an **app
password** (Google Account → Security → App passwords), and paste it into the same box. The password stays in
`.env` and is never shown again. The dashboard then sends each e-mail itself (one click, one personal e-mail, at
most 25 a day) and attaches the 1-page written offer. Without it, the Send button opens your own e-mail app with
the text filled in.

### 2½. 📅 Connect your Google Calendar (10 minutes, free, once)
After this, every deadline shows up on your phone without opening the dashboard:
- **🔥 Follow Up: [street] Agent ([name])** 48 hours after each e-mail to a listing agent (if that lands after 9pm or
  on a Sunday, it waits for 10:00 the next calling day). It carries the agent's phone, e-mail, the asking price and a
  link to the lead card.
- **📞 Call back: [street]** for every other follow-up you schedule by logging a call. The homeowner's number is
  deliberately left off: open the card, which checks the do-not-call lists and calling hours first.
- **⚠️ ATTORNEY REVIEW ENDS: [street]** at 5:00 PM on the 3rd NJ business day after a signed contract (holidays
  such as Election Day skipped), plus **⚠️ FRFPA CANCELLATION ENDS** on covered foreclosures and a **📋** event for
  each contract-to-close step.
- **🚨 SHERIFF SALE AUCTION: [street] ([county] County)** for the 25 sales that matter most (listed foreclosures
  first), reminding you two days and one day before. When a sale is adjourned, the event moves by itself.
- **🎯 Send 15 Daily Wholesaling Outreach Emails (Open Dashboard)** every day at 10:00.

Get the free Google key (you only do this once):
1. Open https://console.cloud.google.com/ while signed in to the Gmail whose calendar you use. Create a project
   (top bar → project picker → New project, any name, e.g. "NJ Wholesale Calendar").
2. **APIs & Services → Library**, search **Google Calendar API**, press **Enable**.
3. **Google Auth platform → Branding**: app name ("NJ Wholesale Cockpit") and your e-mail, Save. **Audience**:
   choose **External**. Then either add your own Gmail under **Test users**, or press **Publish app**. Publishing is
   better: while the app sits in "Testing", Google expires the sign-in every 7 days and you would have to repeat
   step 5 weekly. Because only you use it, Google never needs to review it; you will simply see a "Google hasn't
   verified this app" screen once (Advanced → Go to NJ Wholesale Cockpit).
4. **Google Auth platform → Clients → Create client**, Application type **Desktop app**, Create, then **Download
   JSON**. Rename the file to `credentials.json` and put it in the project's `config/` folder. It is git-ignored;
   never e-mail it or post it.
5. Connect with the Google account whose calendar you actually look at (it can be a different account from the
   one that owns the Cloud project, e.g. a school account; add it under **Audience → Test users** first):
   `python manage.py setup-calendar --account you@school.edu` (or type it in **📅 → Connect Google Calendar**).
   Your browser opens Google's sign-in on that account: allow viewing and editing *events* on your calendars (it
   also confirms which account you are, nothing else: not your e-mail, contacts or calendar settings). Signing in
   with a different account is refused, so the reminders can't land on the wrong calendar. The token is saved to
   `config/token.json` (git-ignored, readable only by you) and the first sync runs. Already connected to another
   account? Run the same command with the new one: the app removes its events from the old calendar and puts them
   on the new one.
   - **School or work account blocked** ("Access blocked", "admin policy", "not approved by your organization")?
     Share that calendar instead: open Google Calendar as the school account → ⚙️ Settings → your calendar under
     "Settings for my calendars" → **Share with specific people** → add your personal Gmail with **Make changes to
     events**. Then run `python manage.py setup-calendar --account you@gmail.com --calendar you@school.edu`. Also set
     that calendar's **Event notifications** (same settings page): reminders on an event belong to whoever made it,
     so your school account notifies you with its own defaults.
   - **Couldn't press "Publish app"?** Then Google ends the sign-in every 7 days. The calendar warns you: a 🔑
     "Re-connect Google Calendar sync" event appears the day before, with the one command to run.

From then on it is automatic: sending an offer, logging a call or recording a contract updates the calendar within
seconds while the dashboard is open, and anything made while you're offline goes up on the next sync (every 10
minutes, or **📅 → Sync now**, or `python manage.py sync-calendar`). Two-way: delete an event in Google Calendar and
it stays deleted; drag a follow-up or the 10:00 reminder to another time and the dashboard's follow-up moves with it;
drag an attorney-review or sheriff-sale date by mistake and it is put back, because those dates are legal facts.
`python manage.py sync-calendar --dry-run` lists what would be created without touching anything.

### 3. Do-Not-Call: register, download, load (before you call any homeowner)
1. Register as a telemarketer with the NJ Division of Consumer Affairs (N.J.S.A. 56:8-119 et seq.). It renews yearly.
2. Create an account at **telemarketing.donotcall.gov**. Your first five area codes are free.
3. `python manage.py dnc --status` lists the five area codes that cover most of your contacts. Download those.
4. `python manage.py dnc --load <downloaded file>`. Repeat every 31 days. The cockpit shows "stale" when it's due.

Listing agents' advertised business numbers are B2B calls, but if any agent says stop, you stop:
`python manage.py agent-blast --opt-out <contact id>`.

### 4. Pick your attorney and title company now, not on deal day
- `python manage.py partners --county Essex` lists NJ title agencies and closing attorneys we checked on their own
  websites. It shows phone numbers and quotes where a firm mentions investors, assignments or double closings.
- Call two of them. Ask: **"Do you insure an assignment of contract, and a same-day double close with separate
  funds?"** Put the one that says yes in `config/nj_closing_partners.yaml` under `preferred:`.
- Hire a NJ real estate attorney once to review the LOI, purchase agreement and assignment drafts in `deals/`
  (`python manage.py docs <id> --type all` makes a set).

### 5. Ask your top towns for their tax-lien lists (free, OPRA)
Click **🏛️ Request Free Town Tax-Lien Spreadsheets (OPRA)**. For each of your top 20 towns you get a ready request
to the town clerk under the Open Public Records Act, the right channel (the clerk's e-mail, or the town's portal
when it only takes portal requests) and the source page we checked.
- Read the three certifications before you send. The request says you **will** use the records for a commercial
  purpose, which is true for house buying and required by law (a false certification is a $1,000+ civil penalty).
  Commercial requests can take up to 14 business days.
- Send it from your own e-mail or paste it into the portal, then click **Mark sent**: the due date is tracked.
- When the tax collector replies with a spreadsheet, upload it on the same page (**1-click import**). Every parcel
  on the list gets a tax-delinquent signal that stacks onto the lead.
- Never use OPRAmachine for these: it publishes the replies, owner names included.

The same page lists **new foreclosure filings** from the county clerks (Union, Essex and Monmouth are read
automatically; Passaic, Hudson, Bergen, Middlesex and Mercer you export in your browser and upload). Filings the
engine can't tie to a house yet have a **clerk record** link: read the property address off the document, type it
in, click **Attach**, and it becomes a lead.

### 6. Optional: your seller and buyer web pages
They run locally at http://127.0.0.1:8000/site/seller and /site/buyers. To host them free on GitHub Pages: create a
free Formspree form, then in the repository's Settings → Secrets and variables → Actions → Variables add
`SITE_COMPANY`, `SITE_OPERATOR`, `SITE_PHONE`, `SITE_EMAIL`, `SITE_POSTAL` and `SITE_FORM_ENDPOINT`, and set Pages →
Source to "GitHub Actions". Every push then rebuilds and publishes the site (`.github/workflows/deploy_site.yml`),
but only if `python manage.py build-site --check` passes: the pages must carry your details, every disclosure, and
no contact details but yours. Import what comes in with `python manage.py inbound --ingest <export>`.

---

## Part 1½ — Phase 0: lock in your three funding partners (before the first offer)

You have no capital and no car, so the people who sign and fund the contracts are rehabbers who already buy in your towns.
The dashboard's 🎯 Autopilot opens with **Phase 0** until three partners are "verified" (proof of funds on file).

1. 🤝 **Partners & POF** → "Seed the top 30 from the deed records". These are named repeat buyers from the county deed exports,
   ranked by purchases, confirmed flips and your priority counties.
2. Work the cards: the e-mail is one plain paragraph (the public deed fact, who you are, the ask) with the JV terms under
   `--- Details ---`. Copy it, send it from your own mail, click **Mark e-mailed**. Text and call scripts are there too.
3. When one says yes: fill the buy box (counties, towns, price band, light/medium/heavy), tick "posts EMD" and "walks houses",
   set the split, upload their proof-of-funds letter and tick "may attach to offers". Status flips to ACTIVE_PARTNER.
4. From then on offers in that buy box say "acquiring with our verified local capital partner", the negotiator can answer a
   proof-of-funds request, and the JV agreement is pre-filled with that partner.

## Part 2a — run it from Claude (no dashboard)

Open the **Claude app → Code**, pick the `realestateauto` folder on your Mac (or run `claude` in Terminal from
`~/realestateauto`), and type:

| You type | What happens |
|---|---|
| `/inbox` | Reads every agent reply in Gmail and sends the offer to everyone who said "still available". That includes "yes it is", "are you a buyer or a realtor?", "want to see it?", "call me" and backup offers. It shows one table: house, what they said, what went back, what you do. Counters and questions wait for your OK. |
| `/daily` | Answers replies first, refreshes the deals, previews today's openers, sends them 25-60 seconds apart (max 25 cold e-mails a day), syncs the calendar. |
| `/status` | Today's numbers, every warm agent thread, what's waiting on you, the next move. |
| `/reply 22 Grand Ave tell him I'll call at 5` | Writes it in your style and sends it in the agent's Gmail thread. Say a price ("…and offer 400k") and the 1-page offer at that price is attached. |

**Hands-free:** run `.venv/bin/python manage.py install-daemon --daily-openers 15` once. From then on, every 5 minutes
between 8 AM and 9 PM, your Mac:
- reads new replies;
- sends the "still available" offers;
- answers "still available" with a no-price question when the numbers need one fact first (a condo's size and HOA,
  a multi-family's rent roll, beds / baths / sqft when the listing matched the wrong house);
- puts counter-offers on your Google Calendar ("🔥 COUNTER-OFFER RECEIVED … Open Claude Chat");
- with `--daily-openers 15`: sends up to 15 new openers / offers each weekday from 10:30 AM, 25-60 seconds apart
  (never more than 25 cold e-mails in 24 hours). Leave the flag off to send them yourself with `/daily`.

Its log is `logs/daemon.log`; `install-daemon --uninstall` stops it.

**New Gmail app password without the dashboard:** create it at https://myaccount.google.com/apppasswords, then run
`.venv/bin/python manage.py set-gmail` and paste it at the prompt (what you type stays hidden; it is tested right away).
Then delete the old one on the same Google page.

**Your calendar is the report:** every day an all-day "📊 Deal desk" event says what went out, what came back and
exactly what waits on you (with the command to run). Replies that need you get a 🔥 event, calls a 📞 / 🔥 follow-up.

**No laptop needed (the cloud desk):** GitHub runs the whole desk every 15 minutes, so the laptop can stay closed.
Turn it on once on github.com: your repository → Settings → Secrets and variables → Actions → New repository secret,
three times:
- `SMTP_USER`: your business Gmail address;
- `SMTP_APP_PASSWORD`: a new Gmail app password (myaccount.google.com/apppasswords);
- `DESK_KEY`: type any 30+ random letters and numbers (you never need it again).
The next time your Mac is open, it hands its database to the cloud by itself and goes quiet. Calls are on your
calendar; everything else happens without you.

**Every 📞 event is a word-for-word call script:** the house, your numbers (open / aim / walk away, or "don't name a
price" and why), exactly what to say first, the questions, what to answer when the agent asks for your offer, whether
you're a wholesaler, proof of funds or a showing, how to end the call, and what never to say. After the call, tell Claude in the chat what they
said ("lead 1368: send the offer", "they took another offer"): it sends the written offer or closes the reply through
the cloud desk.

**Google Voice:** in Voice (voice.google.com → ⚙️ Settings, signed in as your business Gmail) turn on Messages →
"Forward messages to email" and Voicemail → "Get voicemail via email". When an agent texts you or leaves a voicemail,
the desk reads it, puts a 📞 call on your calendar and drafts the text back for you (texts are sent from your phone:
Voice has no way for software to send them, and automated texts aren't allowed here).

**One command for everything:** open Claude Code on the Mac in `~/realestateauto` and type `/go`. It answers every
agent (you say yes once), makes the Mac hands-free, sends today's new e-mails and tells you what's left. Without
Claude: `.venv/bin/python manage.py inbox`, then `.venv/bin/python manage.py gmail --approve-all`.

Commands take the street instead of an id: `gmail --approve "108 S Park"`, `gmail --send-offer "54 N 4th"`,
`gmail --reply "22 Grand" --text "..."`, `gmail --dismiss "12 Juliette"`.

Replies you type yourself in Gmail (on your phone, say) are picked up too, so nothing is ever sent twice.

## Part 2 — every day: one button, then your replies

Double-click the Desktop icon. The home page has three parts, top to bottom:

1. **🚀 RUN TODAY'S AI AUTOPILOT.** Click it once between 8 AM and 8 PM. It e-mails today's top 15 deals from your
   Gmail: urgent listed foreclosures first, then follow-ups that are due, then stale MLS fixers that have an agent
   e-mail. The e-mails go out one at a time, 25-60 seconds apart, so a batch of 15 takes about 10 minutes. You can
   leave the page open and do something else. Each send is logged with its 48-hour follow-up, and the follow-up
   lands on your Google Calendar. Gmail's safe pace is 25 e-mails in 24 hours, and the button won't go past it.
   **⏹ Stop** finishes the current e-mail and stops.
2. **🔥 Action Needed From You.** The dashboard reads your Gmail inbox every 5 minutes. It opens the inbox
   read-only, so nothing gets marked as read. It looks only at replies from agents you wrote to.
   - **"Still available"** with nothing else in it: your priced note and the one-page offer go back by themselves
     in the agent's own thread, a few minutes later. You'll see it under "Handled automatically".
   - **Anything else** waits here for you: a number, a question, proof of funds, an oil tank, tenants, a bidding
     war, "under contract". You get:
     - the agent's message;
     - the traffic light, for example 🟢 **DEAL WORKS AT $470K — Your Est. Profit: $15,900**;
     - a drafted answer.

     Read the draft, then click **✅ Approve & Send AI Reply + LOI**. It answers in their thread with the written
     offer at that price. Or call them with the 📞 button.
   - **"Remove me"** puts the agent on your stop list. A **bounced** e-mail takes the dead address off the card.
   - A Google Calendar alert (🔥 Agent replied) goes up about 10 minutes after a reply that needs you.
3. **📋 Manual Step-by-Step Card Player (optional).** The same one-card-at-a-time deck as before, for phone calls,
   texts or sending by hand. It only shows deals you can reach today: an agent e-mail or a phone that isn't on a
   stop list. Funding partners with no phone or e-mail yet wait in **⚡ Skip Trace** and don't open your day.
   - The e-mail is already written: a short plain note on top, the terms and disclosures under
     `--- Offer Summary ---`. **✉️ Send Email & Log** sends it and moves to the next deal. **📋 Copy Short Text
     Message** copies the text for Google Voice; you always send texts yourself.
   - Calling? Use the 3-sentence script, then log the outcome. **✅ Interested - Send Offer** turns the card into the
     priced note (Touch 2) on the spot: press Send and the one-page offer is attached.

What the button never does:
- negotiate a counter by itself;
- send a price on a deal whose pre-flight shows a condo unit, an ARV above every comp, low confidence, an
  owner-occupied foreclosure (FRFPA) or a payoff problem;
- text anyone;
- send outside 8 AM to 8 PM.

The same from a terminal:
- `python manage.py gmail --dry-run` shows today's batch;
- `--run` sends it;
- `--check` reads the inbox now;
- `--status` shows the counts;
- `--approve <id>` sends an approved reply.

Everything else lives under **Advanced Tools ▾** (top right):
- the full step-by-step deck with the four queues (Urgent Listed Foreclosures, Ready-to-Call MLS Fixers, Cash
  Buyers to Call, Follow-Ups Due Today);
- Partners & POF;
- Skip Trace;
- Today;
- Dashboard;
- Fast Track;
- Ready to Call;
- Agent LOI Blast;
- Inspector & Mail;
- Walkthroughs & Bids;
- Closing & Money;
- Records;
- the lead search.

Once a week, call 5 cash buyers who bought in your towns and get their buy box and proof of funds *before* you have
a deal.

---

## Part 2½ — your 100 calls a day (the four 📞 Call block events)

Every weekday morning the desk puts four **📞 Call block** events on your calendar (one on Saturday). Each has
25 calls and one word-for-word script at the top. The blocks are fitted around your classes. You never pick
who to call: the order is already the money order (agents who replied, foreclosures with a sale coming up,
follow-ups, firm offers, then "ask their number" listings).

1. Open the block and read the script once. Every call line looks like
   `A07 | (732) 555-0100 Maria (Brokerage) | 22 Grand Ave | 3bd/1ba 1,450sf 1955 | ask $389,000, 134 DOM | OFFER $310,000 · best and final $315,000 | lead 4812`.
2. The line tells you the only number you may use:
   - **OFFER $X · best and final $Y**: say $X. If they push, say "My best and final is $Y." Never above $Y.
   - **ASK THEIR NUMBER · yes at or under $Y**: don't say a price first. Ask "What number would the seller sign at
     for cash, as is, with a quick close?" At or under $Y: "That can work, I'll confirm and send it in writing
     today." Above: "That's more than I can pay as is. Mind if I check back in a few weeks?"
   - **NO PRICE**: ask the questions in the script, then "Let me run it and I'll e-mail you a number."
3. After the block, send Claude one line: `A01 vm, A02 na, A03 send, A04 340 roof, A05 cb thu 2pm, A06 stop`.
   - Codes: na (no answer), vm (voicemail), wn (wrong number), stop (asked not to be called), sold, uc (under
     contract), ni (not interested), cb (call back, plus when), send (wants it in writing), or a number (what the
     seller needs: 340 = $340,000).
   - The desk logs every call, e-mails the offers, and puts the call-backs on your calendar.
4. Every offer is final: the e-mail, the written offer (LOI) and your script always carry the same number, and the
   desk never goes back below a number you already sent.

## Part 3 — your first 20 listing-agent calls

Start with the Step-by-Step player (or the Today tab, Block 1, then Block 2). For each one, open the script:
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

## Part 4½ — when the agent writes back: the live negotiator

With Gmail connected (My Setup), agent e-mails reach **🔥 Action Needed** on the home page by themselves, already analyzed;
the paste box below is for texts, voicemails you typed up, or replies that went to another address.

Every deal card and lead page has **"💬 Agent replied or countered? Paste their message here."** Paste the whole e-mail or
text and click Analyze. You get:

* a traffic light: 🟢 **ACCEPT & LOCK UP NOW** (their number is at or under your walk-away; the fee is shown), 🟡 **COUNTER AT
  $X** (keeps a $12,000 fee and explains the gap with the actual repair, tax and carrying numbers), 🔴 **HOLD FIRM / 14-DAY
  DRIP** (the follow-up is scheduled for you), or ℹ️ when there is no number to give (proof of funds asked, under contract,
  FRFPA floor, opt-out);
* the math: their number, your counter, the fee at each, the buyer's profit and ROI, and the risk buffer when they mention an
  oil tank, tenants or a certificate of occupancy (the reply adds the tank-sweep / lease-and-deposit / certificate lines);
* a three-sentence reply and a text, already in your voice, with the Offer Summary underneath. Copy it or open it in e-mail.

"Still available?" answers from your two-line opener come back as **They replied: send the offer** cards: that is Touch 2, the
priced note. A bidding war never moves you above the walk-away. "Remove me" puts the agent on your stop list on the spot.

**Two-step funnel:** by default a listing agent you have never written to gets the two-line opener (no price, nothing to mark
as spam); the priced note goes out when they answer. Switch "First touch" in the Autopilot header to "priced note first" if
you prefer. Listed foreclosures with a sale inside 14 days always get the direct auction-date packet.

**⚡ Skip Trace** (owners who are not listed): the queue is ordered by who converts from a desk (exempt estates, vacant sheriff
sales, lis pendens, absentee owners). Click the lookup link, select the whole result page, copy, come back and click
**Paste raw page**. Numbers are checked against the FTC data and your stop list, the best mobile is ranked first, contacts
are saved and the lead turns Ready-to-Call. The sites are never fetched by the app.

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

## Part 5½ — the contract says "no assignment" (Item 14 / Section 37), and assignment vs double close

Most listing agents use the NJ REALTORS form, which has a "No Assignment" section (Section 37 today, Item 14 on the old NJAR
form). Do not argue about it on the phone: generate the **Item 14 rider** (Documents → "Item 14 rider (attorney review)") and
have your attorney submit it during the three-day attorney review. It replaces that section with a right to assign to a
single-purpose LLC or an affiliated investment partner, asks for the seller's property-condition disclosure (required by NJ law
since August 2024) and the flood addendum, sets a 10-business-day inspection period with an underground oil tank sweep (seller
removes it or money is held in escrow), lets your inspectors, partners and assignees in on 24 hours' notice, and tells the seller
in plain words that you are an investor who may resell or assign for profit. Nothing is hidden from the seller, ever.

The lead page's **Assignment vs double close** card shows the net under both. The rule of thumb it applies: assign when the
spread is under $30,000 and the seller accepted the rider; double close (two same-day closings funded by a transactional lender
at about 1% plus fees, you pay the transfer fee on the resale deed) when the seller refused the rider or the spread is large
enough that a fee on the settlement statement would blow up the deal. The disclosed single-purpose LLC route is explained
under the card; it needs your attorney and a cash end buyer.

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

## When something goes wrong

- **A red message after ✉️ Send or 🚀:** it now says what happened in one sentence. Nothing was sent.
  - *"Gmail refused the sign-in"*: you need a 16-letter **app password** made on the same Google account as the
    "Gmail address to send from" (Google Account > Security > 2-Step Verification > App passwords). Paste it into
    ⚙️ My Setup; spaces are fine.
  - *"couldn't verify Gmail's security certificate"*: run `cd ~/realestateauto && .venv/bin/pip install -U certifi`,
    then restart the dashboard.
  - *"database is busy"*: a data refresh is writing. Wait a minute, or open 🤖 Run Autopilot Data Refresh and press
    **⏹ Stop refresh**.
- **Check Gmail before a big batch:** click **🔌 Test Gmail** on the home page, or run
  `.venv/bin/python manage.py gmail --test`. It signs in to sending (SMTP) and reading (IMAP) without sending
  anything, and shows ✅ / ❌ for each with the reason. Add `--send-test` to send yourself a two-line note.
- **An agent said "still available" but no offer went out yet:** open that house (Advanced Tools ▾ → search the
  address). The **✉️ Next e-mail to the agent** box at the top of the middle column shows the offer. Press
  **✉️ Send the offer from Gmail now**: it goes in their thread with the 1-page offer. On the home page, a house
  under "⏳ Going out by themselves" has a **⏩ Send the offer now** button.
- **Anything else:** the full error is in `logs/server.log` in the project folder. Send the last 30 lines when you
  ask for help (`tail -30 ~/realestateauto/logs/server.log`).

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
