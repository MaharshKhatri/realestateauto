# Inbound funnel (seller + cash-buyer pages)

Two static pages, no build step, no tracking scripts:

- `index.html` — "Sell your New Jersey house as-is" seller form
- `buyers.html` — cash-buyer registration (buy box, counties, proof of funds)

Both read `config.js` (`window.SITE_CONFIG`): company, phone, e-mail, postal address, consent text and how forms
are delivered. `config.js` is generated, never committed.

## Run it locally (forms go straight into the database)

```bash
python manage.py server          # then open http://127.0.0.1:8000/site/seller and /site/buyers
```

Submissions `POST /api/inbound/seller` and `/api/inbound/buyer`: the seller becomes a lead (parcel match,
underwriting, drafted reply, follow-up due now); the buyer joins `cash_buyers` and is matched to deals.

To take submissions from the internet while the cockpit runs on your laptop, put a tunnel in front of it and list
the tunnel's host name under `web.public_hosts` in `config/settings.local.yaml`. Requests from those hosts can
only reach `/site/*` and `POST /api/inbound/*` — never the cockpit or the lead data.

## Host it for free (GitHub Pages / Netlify / Vercel)

```bash
python manage.py site --build site_dist --endpoint https://formspree.io/f/<your-form-id>
```

`site_dist/` gets the pages plus a `config.js` with your details. Upload that folder. Without `--endpoint` the
forms open the visitor's e-mail app addressed to you ("mailto" mode).

Collect what came in: export the Formspree submissions (JSON or CSV), or save an e-mail body, then

```bash
python manage.py inbound --ingest submissions.json
```

## Rules the pages follow

- Investor / principal disclosure, "may assign the contract", attorney review — on the page and in every reply.
- Foreclosure: we can't stop, postpone or negotiate it; free HUD and Legal Services numbers; FRFPA protections.
- No testimonials, statistics or timelines we can't prove.
- Phone contact only with the visitor's checkbox consent; STOP always works (`manage.py dnc --add`).
