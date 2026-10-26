# NJ Wholesaling System: Legal Rules Verification

**Prepared:** 2026-10-26 by research agent (not legal advice; for engineering requirements only, so have NJ counsel review before production use).
**Scope:** Legal rules that the wholesaling software, call/text scripts and contract templates encode.
**Method:** Primary sources first: P.L. chapter laws on `pub.njleg.gov`, court opinions on `njcourts.gov` / `cases.justia.com` / `cdn.ca9.uscourts.gov`, NJ Treasury/FTC/eCFR pages. Where only a secondary source was reachable, it is labelled. Items I could not confirm are marked **VERIFY:**.

> **Source-access note.** `law.justia.com/codes/new-jersey/...` returned HTTP 403 to automated fetches in this session, and `lis.njleg.state.nj.us` statute search could not be queried. Justia code links are still given below as canonical citations. The text was checked against (a) the enacting/amending **P.L. chapter law** on `pub.njleg.gov`, which is primary, or (b) the `law.onecle.com` mirror, which is secondary and can be out of date. Each place that relied on the mirror is flagged, and current text should be confirmed on Westlaw/Lexis or the Legislature's statute database.

---

## Contents
1. [Attorney review](#1-attorney-review)
2. [Tax sale certificates (N.J.S.A. 54:5)](#2-tax-sale-certificates-njsa-545)
3. [Mortgage foreclosure timeline and sheriff sales](#3-mortgage-foreclosure-timeline-and-sheriff-sales)
4. [Consumer Fraud Act (and the Foreclosure Rescue Fraud Prevention Act)](#4-consumer-fraud-act-njsa-568-1-et-seq)
5. [Wholesaling legality and licensing](#5-wholesaling-legality--licensing-in-nj)
6. [Telemarketing (TCPA + NJ)](#6-telemarketing)
7. [Daniel's Law](#7-daniels-law)
8. [Realty transfer fee and "mansion tax" (Graduated Percent Fee)](#8-nj-realty-transfer-fee--mansion-tax-graduated-percent-fee)
9. [Rules the software must enforce (checklist)](#9-rules-the-software-must-enforce-checklist)
10. [Corrections](#10-corrections)
11. [Open VERIFY items](#11-open-verify-items)

---

## 1. Attorney review

### Rule in plain English
A NJ real estate **licensee** (broker, broker-salesperson, salesperson) may prepare a contract to sell a **1–4 family residence** or a **vacant one-family lot** (and residential leases of 1 year or more) only if the contract contains the court-mandated **3-business-day attorney-review clause**. During that window either party's attorney can disapprove, and the contract is then void. If a licensee prepares the contract without the clause, the licensee is engaged in the unauthorized practice of law.

### Origin and citations
- **New Jersey State Bar Ass'n v. New Jersey Ass'n of Realtor Boards**, 93 N.J. 470 (1983), *modified*, 94 N.J. 449 (1983). The Supreme Court approved a consent judgment, with modifications, settling the Bar's unauthorized-practice suit against realtors. The modification at 94 N.J. 449 allowed the term "Realtor" to replace "Broker" in the clause. *Source:* both opinions below quote and describe the consent judgment.
  - The judgment's scope language, quoted in *Sullivan*: licensees "shall be permitted to prepare contracts for the sale of residential real estate containing one-to-four dwelling units and for the sale of vacant one-family lots in transactions in which they have a commission or fee interest, PROVIDED that every such contract shall contain conspicuously at the top of the first page" the notice "THIS IS A LEGALLY BINDING CONTRACT THAT WILL BECOME FINAL WITHIN THREE BUSINESS DAYS…" Preparation "except as provided herein shall constitute the unauthorized practice of law."
  - **Date of the modification:** the user brief calls it a "1984 modification." The NJ Supreme Court itself cites it as **94 N.J. 449 (1983)** in *Conley* and *Sullivan*. Use the 1983 date unless counsel says otherwise.
- **Regulation:** **N.J.A.C. 11:5-6.2(g)** (Real Estate Commission), added by R.1987 d.159, effective 4/6/1987, codifies the clause for all licensees ([LII text](https://www.law.cornell.edu/regulations/new-jersey/N-J-A-C-11-5-6-2)). Under (g)(7), omitting the language is treated as unauthorized practice and as "unworthiness/incompetency" under N.J.S.A. 45:15-17(e).
- **Conley v. Guerrero**, 228 N.J. 339 (2017) ([opinion PDF](https://cases.justia.com/new-jersey/supreme-court/2017-a-65-15.pdf), A-65-15, decided Apr. 3, 2017). This is the controlling delivery-method decision.
- **Sullivan v. Max Spann Real Estate & Auction Co.** (A-57-20, decided June 9, 2022) ([opinion PDF](https://cases.justia.com/new-jersey/supreme-court/2022-a-57-20.pdf)). No attorney-review clause is required in a contract signed after an **absolute auction** when bidders received pre-auction notice. The opinion also restates the consent-judgment scope.
- **Peterson v. Estate of Pursell**, 339 N.J. Super. 268, 273-75 (App. Div. 2001), as described in *Conley*. The period runs from **delivery of a conforming signed contract to the party**, not to the party's agent.
- **Romano v. Chapman**, 358 N.J. Super. 48 (App. Div. 2003), as described in *Conley*. Once an attorney approves, the contract is binding even if the attorney later tries to disapprove before the deadline.

### Covered contracts
- Contracts **prepared by a licensee** who has a **commission or fee interest** in a sale of:
  - residential property with **1–4 dwelling units**, or
  - a **vacant one-family lot**.
- Residential **leases of 1 year or more**, which use a parallel clause.
- Not covered: absolute-auction sales with proper notice (*Sullivan*); contracts not prepared by a licensee (see below).

### Counting the 3 business days
Required clause text, from N.J.A.C. 11:5-6.2(g)(2) as summarized by LII and *Conley*:
- **"Study by Attorney":** the review must be completed within 3 days.
- **"Counting the Time":** "You count the three days from the date of delivery of the signed contract to the Buyer and the Seller. You do not count Saturdays, Sundays or legal holidays." The parties may agree **in writing** to extend.
- **Start trigger:** delivery of the **fully signed** contract to **both** buyer and seller. Delivery to a broker or agent alone does not start the clock (*Peterson*). The delivery day is day 0 and the next business day is day 1.
- **Holidays:** "legal holidays" are NJ legal holidays. N.J.S.A. 36:1-1 lists New Year's Day, MLK Day, **Lincoln's Birthday (Feb 12)**, Washington's Birthday, **Good Friday**, Memorial Day, July 4, Labor Day, Columbus Day, Veterans Day, Thanksgiving, Christmas, **any general election day**, and Governor/President-proclaimed days. The text checked was the onecle mirror. **VERIFY:** the current list, including Juneteenth (believed added 2020) and any later additions.
- **End time:** the clause does not state a clock time. **VERIFY:** treat the period as ending at the end of the 3rd business day. Operationally, assume the contract is not binding until the next business day.
- **Worked example:** signed contract delivered to both parties **Fri Nov 6, 2026**. Business day 1 is Mon Nov 9, day 2 is Tue Nov 10, Wed Nov 11 (Veterans Day) is skipped, and day 3 is **Thu Nov 12**. The contract becomes binding after Thu Nov 12 if no disapproval was sent.

### How disapproval must be delivered
- **Regulatory text (N.J.A.C. 11:5-6.2(g)(2)), still on the books:** notice goes to **the Broker(s) and the other party** within the 3 days. It must be sent "by certified mail, by telegram, or by delivering it personally." Certified mail and telegram are effective upon sending. Personal delivery is effective upon delivery to the broker's office. Under (g)(3), the contract must list names and full addresses for notice.
- ***Conley v. Guerrero* (2017) overrides the method:** using its constitutional authority over the practice of law, the Court held that disapproval "may be transmitted by **fax, e-mail, personal delivery, or overnight mail with proof of delivery**. Notice by overnight mail will be effective upon mailing. The attorney-review period within which this notice must be sent remains three business days." Actual notice received within the period by a method commonly used in the industry was valid.

### Does it apply when no licensee is involved (wholesaler-drafted contract)?
- **Generally no.** The consent judgment and N.J.A.C. 11:5-6.2(g) regulate **licensees**. A purchase contract drafted by the wholesaler as **principal**, or by an attorney, is not legally required to contain the attorney-review clause. No NJ statute imposes attorney review on unlicensed principals.
- **It does apply** if any NJ licensee prepares or fills in the contract with a fee or commission interest. Examples: the wholesaler or an employee holds a license, or a licensed agent on the dispo side drafts it.
- **Other cancellation regimes can still apply:**
  - Foreclosure Rescue Fraud Prevention Act: a **10-business-day** cancellation right for distressed owner-occupied 1–4 unit properties (see §4).
  - Pending **A4278 (2026)**: a **30-day** cancellation right for "wholesale transactions" (see §5).
- **VERIFY:** if a licensee acts as principal (buying for its own account), whether the "commission or fee interest" condition is met is unsettled. A separate rule, N.J.A.C. 11:5-6.4, requires licensees to disclose their licensed status when dealing as principal; confirm the subsection with counsel.

### What the software and scripts must do
1. Store `prepared_by_licensee` (bool) and `licensee_fee_interest` (bool) on every contract. If both are true and the property is 1–4 units or a vacant one-family lot, **block** contract generation unless the mandatory header and attorney-review clause are inserted verbatim from N.J.A.C. 11:5-6.2(g).
2. Compute `attorney_review_end` as 3 NJ business days after the later of the delivery timestamps to buyer and to seller. Exclude Saturdays, Sundays and N.J.S.A. 36:1-1 holidays, using a maintained, configurable holiday table.
3. Do not mark the deal "binding" or "under contract", trigger dispo marketing, or order paid title/inspections until `attorney_review_end` passes with no disapproval logged. Exception: an attorney approval letter has been recorded (*Romano*).
4. Accept disapproval by fax, e-mail, personal delivery, overnight mail (effective on mailing), certified mail or telegram. Record channel, timestamp and proof of delivery.
5. For wholesaler-as-principal contracts, set the flag to `attorney_review: not legally required` but still offer a voluntary review clause. Separately evaluate FRFPA (§4) and pending A4278 (§5).

---

## 2. Tax sale certificates (N.J.S.A. 54:5)

### Rules in plain English
- **Auction and interest:** the municipality sells a lien, in the form of a certificate, on unpaid taxes and municipal charges. Bidders **bid down the interest rate from 18%**. If a bidder goes below 1% (to 0%), bidding switches to a **premium** paid above the lien amount, and the highest premium wins.
- **Redemption:** the owner, mortgagee and others may redeem **at any time until a final judgment** bars redemption.
- **Foreclosure timing:** a third-party holder may **file** to foreclose only **after 2 years** from the sale. A municipality (or its assignee) may file **after 6 months**. Abandoned property may be filed on **at any time** with a certification.
- **Surplus equity (2024 amendment):** since **P.L.2024, c.39 (effective July 10, 2024)**, an owner (or heirs) may demand, before final judgment, that the foreclosure proceed by **sheriff's sale or Internet auction** so surplus equity is preserved. If no demand is made, the holder may foreclose without a sale and the owner loses the equity claim.

### Citations and exact numbers
| Topic | Rule | Citation |
|---|---|---|
| Max rate / bid-down | Sale "subject to redemption at the lowest rate of interest, but in no case in excess of **18% per annum**." Below **1%**, bid a **premium**; the highest premium wins. | N.J.S.A. 54:5-32 ([Justia](https://law.justia.com/codes/new-jersey/title-54/section-54-5-32/); text via onecle mirror; last amended P.L.2009, c.320) |
| Municipal default bid | Parcels with no other bidder are struck off to the municipality at **18%**. | N.J.S.A. 54:5-34 |
| Premium | Held by the collector and returned to the purchaser on redemption (**without interest**, per municipal guidance). It escheats to the municipality if not redeemed within **5 years** of sale; the period is extended for bankruptcy-stay days. **New 2024 rule:** in the sheriff-sale/auction path, the premium is refunded if the **writ of execution is sent to the sheriff within 5 years**, but **not** if the holder or its assignee is the successful bidder. Premiums due to escheat in 2024 were extended 1 year. | N.J.S.A. 54:5-33 as amended by [P.L.2024, c.39 §1](https://pub.njleg.gov/bills/2024/PL24/39_.HTM) |
| Redemption within 10 days | Within 10 days of sale (sale day counts as day 1), unless a certificate was issued in that window: amount paid plus interest at the bid rate. After that: plus expenses and subsequent liens. | N.J.S.A. 54:5-58 (onecle mirror) |
| Redemption penalty | On redemption the holder also collects a percentage of the **certificate amount**: **2%** if it exceeds **$200**, **4%** if it exceeds **$5,000**, **6%** if it exceeds **$10,000**. Municipal guidance says the penalty is assessed **only on redemption, not on foreclosure**. | N.J.S.A. 54:5-61 (onecle mirror; last amended P.L.2001, c.72); Haddonfield/Robbinsville collector guidance |
| Holder expenses | Search fees capped at **$12** total, plus recording cost actually paid and necessary advertising/service costs (54:5-61). After the pre-complaint notice: title/foreclosure search ≤ **$350**, attorney fee ≤ **$150**, certified-mail costs, and municipal calculation fee. After a complaint is filed: **$2,500** attorneys' fee deemed reasonable, plus enumerated costs (search ≤ $350, update ≤ $100, etc.). | N.J.S.A. 54:5-61; 54:5-98(b)–(c) as amended by P.L.2024, c.39 §5 |
| Pre-complaint notice | At least **30 days'** written notice by certified mail before filing (a condition of fee recovery). The notice must state the redemption amount and the owner's **right to request a sheriff sale/Internet auction**. Municipal calculation fee ≤ **$50**. | N.J.S.A. 54:5-97.1 as amended by P.L.2024, c.39 §4 |
| Interest on subsequent taxes | A third-party holder who pays later taxes/liens (and files an affidavit with the collector) recovers them plus interest **"at the rate or rates chargeable by the municipality"**. That is the municipal delinquency rate, **≤ 8% on the first $1,500 and ≤ 18% on the excess**, not the certificate bid rate. | N.J.S.A. 54:5-60; 54:4-67(a)(1) (onecle mirror) |
| Year-end penalty | A municipality may impose up to **6%** on delinquencies **over $10,000** not paid by fiscal-year end. A holder who pays the delinquency before year-end is entitled to the penalty on redemption; otherwise it gets a pro-rata share. | N.J.S.A. 54:4-67 |
| Redemption calculations | The collector provides **2 free** redemption calculations per calendar year; later ones cost ≤ **$50** by ordinance. Requests must be in writing. | N.J.S.A. 54:5-54 |
| Who may foreclose and when | Municipality or its assignee: **after 6 months** from the sale. Others: **after 2 years** from the sale. Abandoned property (P.L.2003, c.210): **any time**, with a public officer/collector certification. Redemption continues "until barred by the judgment of the Superior Court, except as set forth in R.S.54:5-87." | N.J.S.A. 54:5-86 as amended by P.L.2024, c.39 §2 |
| Finality | No application to reopen a final judgment after **3 months**, and then only for **lack of jurisdiction or fraud**. | N.J.S.A. 54:5-87(a) |
| In rem | Municipal-only summary foreclosure under the In Rem Tax Foreclosure Act, P.L.1948, c.96 (**N.J.S.A. 54:5-104.29 et seq.**), started by governing-body resolution and a "tax foreclosure list" (54:5-104.35). Since 2024 the owner/heirs may demand a judicial sale/auction before final judgment; a multi-schedule complaint is then severed. | N.J.S.A. 54:5-104.35, -104.64(d) as amended by P.L.2024, c.39 §9 |

### 2024 amendments after *Tyler v. Hennepin County*
- ***Tyler v. Hennepin County*, 598 U.S. 631 (2023):** keeping surplus equity beyond the tax debt is a taking.
- **P.L.2024, c.39 (A3772 1R), approved July 10, 2024, effective immediately** ([text](https://pub.njleg.gov/bills/2024/PL24/39_.HTM)):
  - **Applies to:** any lien not yet foreclosed on July 10, 2024. **No effect** on foreclosures with a final judgment entered before that date (§11).
  - **Owner's demand right (54:5-87(b)):** in actions under 54:5-86(a) (non-abandoned property), the owner or heirs may demand **in writing to the Superior Court before final judgment** a **judicial (sheriff's) sale** or a **sheriff's Internet auction**. Without a demand, the holder forecloses without a sale and "the owner and the owners heirs shall have no claim … for any equity."
  - **Price presumptions:** the sale price is **conclusively presumed** to be fair market value. If no one bids, it is conclusively presumed there is no equity.
  - **Surplus:** the sheriff deposits surplus with the Superior Court clerk after the holder is paid the redemption amount, costs and fees. Interest keeps accruing on the certificate under 54:4-67 until paid. Surplus claims proceed under **N.J.S.A. 2A:50-37** and court rules. This is the **exclusive** method for owner surplus claims.
  - **Holder's first lien on surplus (C.54:5-98.1(b)):** where the owner demanded a sale, the holder gets **10% of the surplus, capped at $5,000**, for administrative costs, plus a first lien for unrecovered costs and fees.
  - **Mandatory notices:** the summons and complaint must include a **bold-face** notice of the sale-demand right (C.54:5-98.1(a)). Municipal in rem notices must state it conspicuously (C.54:5-98.2). Owners in actions pending on enactment had to be served the notice by regular and certified mail (§10).
  - **Abandoned property:** actions under 54:5-86(b) do **not** require a sale. The judgment bars "claims to surplus equity."
- ***257-261 20th Ave. Realty, LLC v. Roberto***, 259 N.J. 417 (2025) (A-29-23, decided **Jan. 9, 2025**) ([opinion](https://www.njcourts.gov/system/files/court-opinions/2025/a_29_23.pdf)):
  - The pre-2024 Tax Sale Law is **unconstitutional to the extent it allows forfeiture of surplus equity without just compensation**.
  - **Private lienholders can be state actors.**
  - Lienholders are entitled to "the value of tax sale certificates they purchased … along with interest and related costs," but **not surplus equity**.
  - The Court did not decide whether the 2024 law itself is constitutional.
- ***Pung v. Isabella County*, No. 25-95 (U.S. June 23, 2026):** per law-firm summaries ([Faegre Drinker](https://www.faegredrinker.com/en/insights/publications/2026/6/supreme-court-decides-pung-v-isabella-county-michigan), [Holland & Knight](https://www.hklaw.com/en/insights/publications/2026/08/pung-v-isabella-county-clarifies-the-post-tyler-tax-foreclosure)), the just-compensation baseline is the **tax-sale price when the sale is fairly conducted**, not hypothetical market value. **VERIFY** against the slip opinion.

### Worked redemption example (illustrative; the collector's certified figure always controls)
**Assumptions:**
- Sale on **Oct 15, 2025**. Certificate amount **$12,000**, bid at **9%**. Certificate recorded (recording cost **$40**, assumed) plus **$12** search fees.
- Holder paid subsequent taxes of **$2,000 on Feb 1, 2026** and **$2,000 on May 1, 2026**, with affidavits filed.
- Municipal delinquency rate is 8% on the first $1,500 and 18% above. **VERIFY** how the collector applies the $1,500 tier to subsequent liens.
- No pre-complaint notice and no complaint filed.
- Owner redeems on **Jan 15, 2027** (15 months after sale). Simple interest, month-based.

| Component | Calculation | Amount |
|---|---|---|
| Certificate principal | | $12,000.00 |
| Certificate interest | 12,000 × 9% × 15/12 | $1,350.00 |
| Redemption penalty (cert > $10,000 → 6%) | 12,000 × 6% | $720.00 |
| Subsequent taxes paid | 2,000 + 2,000 | $4,000.00 |
| Interest on 1st payment: first $1,500 @ 8% | 1,500 × 8% × 11.5/12 | $115.00 |
| Interest on 1st payment: remaining $500 @ 18% | 500 × 18% × 11.5/12 | $86.25 |
| Interest on 2nd payment @ 18% | 2,000 × 18% × 8.5/12 | $255.00 |
| Recording cost + search fees | 40 + 12 | $52.00 |
| **Total to redeem** | | **$18,578.25** |

**Variants:**
- After a complaint is filed, add the **$2,500** statutory attorneys' fee plus sworn costs (54:5-98(c)).
- If the certificate had sold at **0% with a $5,000 premium**: certificate interest = $0; the collector separately returns the $5,000 premium **without interest**; the 2/4/6% penalty is still computed on the certificate amount. **VERIFY:** whether the penalty applies to premium (0%) certificates; local practice reportedly says yes.

### What the software must do
1. Model the certificate as `{sale_date, cert_amount, bid_rate (0–18%), premium, holder_type: municipal|third_party, recorded_costs}`. Validate `0 ≤ bid_rate ≤ 18`; `premium > 0` only if `bid_rate == 0` (bid below 1%).
2. Redemption calculator: compute interest on the certificate at `bid_rate`; apply the **2%/4%/6%** penalty with thresholds **>$200 / >$5,000 / >$10,000** on the certificate amount; accrue subsequent-tax interest at the **municipal delinquency rate (8%/18%)**, not `bid_rate`; add capped costs. Label the output **"estimate – collector's certified redemption statement controls."**
3. Earliest foreclosure filing date: `sale_date + 6 months` (municipal/assignee), `+ 2 years` (third party), or immediately with an abandoned-property certification. Never show a "redemption expires" date. Show **"redeemable until final judgment"** instead.
4. For each lien foreclosure, track the owner's sheriff-sale-demand window (before final judgment) and the 3-month reopen window after judgment. Flag post-July 10, 2024 cases as "surplus-equity protected."
5. Track the **5-year premium clock** (sale date + 5 years, extended for bankruptcy days) and whether the writ was sent to the sheriff within it.

---

## 3. Mortgage foreclosure timeline and sheriff sales

### Step-by-step rules and numbers
| Stage | Rule | Citation |
|---|---|---|
| **Notice of Intention to Foreclose (NOI)** | Required before accelerating or filing on a **residential mortgage**. Must be sent **at least 30 days but not more than 180 days** before filing. If **more than 180 days** pass without filing, a **new** NOI is required. Sent by **registered or certified mail, return receipt requested**, to the last known address and the property address. Effective on the date delivered in person or mailed. **Content:** obligation; nature of the default; right to cure; cure amount; cure-by date **≥ 30 days** after notice; consequences; right to cure continues post-filing (debtor pays capped fees/costs); right to transfer and transferee's cure right; advice to seek counsel (Bar/Lawyer Referral/Legal Services); list of DOBI assistance programs; lender contact for disputes; **Foreclosure Mediation Program** option; **free housing counseling**; and receivership duty for poorly maintained owner-occupied 2–4 unit properties. Not required if the property was voluntarily surrendered. Compliance must be pleaded. | N.J.S.A. 2A:50-56 as amended by [P.L.2019, c.69](https://pub.njleg.gov/bills/2018/PL19/69_.HTM) (S3411 2R). Secondary sources say P.L.2019, c.134 requires filing the NOI with DCA from Apr. 1, 2020: **VERIFY** |
| **Right to cure** | The debtor may cure and reinstate **any time up to entry of final judgment**, but only **once every 18 months** (unless cured by the NOI date). | N.J.S.A. 2A:50-57 (onecle mirror) |
| **Lis pendens** | After filing the complaint, the plaintiff files a notice of pendency with the county clerk/register (title, object, property description). Not allowed for money-only actions. | N.J.S.A. 2A:15-6 ([Justia](https://law.justia.com/codes/new-jersey/title-2a/section-2a-15-6/); onecle mirror) |
| **Answer** | Defendant must answer within **35 days** of service of summons and complaint. | R. 4:6-1(a) (text via [courtrules.net mirror](https://www.courtrules.net/new_jersey/nj-civil/rule-4-6-1); **VERIFY** on the official NJ Court Rules) |
| **Mediation** | Eligible: **owner-occupied 1–4 unit** homes. The court may order mediation when an answer is filed, or the homeowner may initiate it; courts must allow **no less than 60 days** after receipt of the complaint to initiate. Requires a HUD-certified counselor certification. **No fee** to the homeowner. Sanctions for non-attendance up to **$1,000**. Plaintiff pays **$155** per foreclosure filing into the mediation fund. | N.J.S.A. 2A:50-75 to -80, [P.L.2019, c.64](https://pub.njleg.gov/bills/2018/PL19/64_.HTM) |
| **Office of Foreclosure / uncontested track** | Uncontested matters (no answer, or answers stricken or made non-contesting) go to the **Office of Foreclosure** (AOC, Trenton), which **recommends** final judgment to a designated Superior Court judge. A specific objection to the amount due refers the case back to the Chancery judge in the county of venue. Contested matters stay with the Chancery judge. | R. 1:34-6; R. 4:64-1(c),(d) (secondary sources and [App. Div. opinions](https://www.njcourts.gov/system/files/court-opinions/2024/a1485-22.pdf)); **VERIFY** current text |
| **Pre-judgment 14-day notice** | In uncontested cases the lender mails a notice **≥ 14 calendar days** before submitting proofs for final judgment. If the debtor certifies within **10 days** a reasonable likelihood of curing within **45 days**, judgment proofs cannot be returnable before **day 46**. | N.J.S.A. 2A:50-58 (onecle mirror) |
| **Final judgment and writ** | The final judgment orders sale. A writ of execution issues to the county sheriff. The sheriff must **conduct the sale within 150 days** of receiving the writ (raised from "schedule within 120 days" by P.L.2019, c.71). If the sheriff can't comply, plaintiff may obtain a **Special Master**. | N.J.S.A. 2A:50-64(a)(3) as amended by [P.L.2019, c.71](https://pub.njleg.gov/bills/2018/PL19/71_.HTM) |
| **Sale notice** | Posting at the sheriff's office and the premises **≥ 3 weeks** before sale. Publication **4 times, once a week for 4 consecutive weeks** in 2 newspapers, first **≥ 21 days** and last **≤ 8 days** before the sale. Mailed notice (certified/registered) to appearing parties and the record owner **≥ 10 days** before the sale, with a surplus-money notice and a tenants' rights notice. **Upset price** must be posted **≥ 4 weeks** before the sale and may not rise more than **3%** by sale day (CWPP). | N.J.S.A. 2A:61-1 (onecle mirror); R. 4:65-2 ([mirror](https://www.courtrules.net/new_jersey/nj-civil/rule-4-65-2)); N.J.S.A. 2A:50-64(a)(5) as amended by [P.L.2023, c.255](https://pub.njleg.gov/bills/2022/PL23/255_.HTM) |
| **Bidding / deposit** | The successful bidder pays a **20%** deposit immediately or the sale is voided and resold. **CWPP exception:** the foreclosed owner, next of kin, tenant, a nonprofit CDC, or a bidder who will owner-occupy **≥ 84 months** pays **3.5%** of the (lower) upset price, with the balance due in **90 business days**. The owner, next of kin or tenant has a **right of first refusal** at the upset price. | N.J.S.A. 2A:50-64(a)(7), (d) (P.L.2023, c.255, approved Jan. 12, 2024) |
| **CWPP nonprofit right of second refusal** | **Held unconstitutional as applied** (Takings Clause; deprives owners of surplus equity and junior lienholders of surplus funds): *Atlantic County Sheriff's Office v. State*, A-1098-25 (App. Div. **July 10, 2026**, unpublished, per curiam) ([opinion](https://www.njcourts.gov/system/files/court-opinions/2026/a1098-25.pdf)), affirming the Chancery order of Aug. 28, 2025. **VERIFY:** further review or legislative fix (S4470/A5702 reported pending). | N.J.S.A. 2A:50-64(g) |
| **Post-sale 10 days** | The sheriff delivers the deed **unless a motion objecting to the sale is served within 10 days after the sale, or any time before delivery of the conveyance**. Late objections require a valid ground (fraud, accident, surprise, irregularity, inadequate price). The **mortgagor has an absolute right to redeem during the 10 days after the sale** (*Brookshire Equities v. Montaquiza*, 346 N.J. Super. 310, 315 (App. Div. 2002); *Hardyston Nat'l Bank v. Tartamella*, 56 N.J. 508 (1970)). A bankruptcy filing extends this to **60 days** (11 U.S.C. § 108(b)). The right does **not** extend to deed delivery absent a valid objection. | R. 4:65-5 ([mirror](https://www.courtrules.net/new_jersey/nj-civil/rule-4-65-5)); *MSND Financial, LLC v. 187 Loveladies Holdings, LLC*, A-1744-23 (App. Div. July 22, 2024, unpublished) ([opinion](https://www.njcourts.gov/system/files/court-opinions/2024/a1744-23.pdf)) |
| **Deed** | Plaintiff's attorney delivers the deed to the sheriff within **10 days** of the sale. The sheriff delivers the executed deed **within 2 weeks** of the sale if the balance is paid. | N.J.S.A. 2A:50-64(b)–(c) (P.L.2019, c.71 text) |
| **Surplus funds** | Surplus is deposited with the court and paid to those entitled "upon application therefor." Claims are made by motion under R. 4:64-3 / R. 4:57-2 (Superior Court Trust Fund). AG Consumer Fraud settlements (Oct. 15, 2015) with **4** surplus-recovery firms totalled **$309,673** for misrepresenting that owners needed their help. | N.J.S.A. 2A:50-37; [AG release](https://www.njoag.gov/?p=49951) |
| **Post-deficiency redemption** | If a deficiency judgment is sought, the debtor has a separate redemption right within **6 months** of the deficiency judgment (secondary sources). **VERIFY.** | N.J.S.A. 2A:50-4 |

### Sheriff sale adjournments: N.J.S.A. 2A:17-36 (verified statute text)
Current text, as enacted by **P.L.2019, c.71, § 2** (S3464 1R, **approved April 29, 2019, effective on the 90th day after enactment = July 28, 2019**) ([chapter law](https://pub.njleg.gov/bills/2018/PL19/71_.HTM)):

> "2A:17-36. Adjournments of sale of real estate. Notwithstanding any other law or court rule to the contrary, a sheriff or other officer selling real estate by virtue of an execution may make **five adjournments** of the sale, **two at the request of the lender, two at the request of the debtor, and one if both the lender and debtor agree** to an adjournment, and no more, to any time, **not exceeding 30 calendar days for each adjournment**. However, a court of competent jurisdiction may, for cause, order further adjournments."

Prior text (L.1995, c.244, § 14), shown in the [S3464 1R bill](https://pub.njleg.gov/bills/2018/S3500/3464_R1.HTM) and quoted in *Lazarus v. Tawil*:

> "A sheriff or other officer selling real estate by virtue of an execution may make **two adjournments** of the sale, and no more, to any time, **not exceeding 14 calendar days** for each adjournment. However, a court of competent jurisdiction may, for cause, order further adjournments."

**Which number is correct, 14 or 28 days?** **Neither.** Since **July 28, 2019** each statutory adjournment may be up to **30 calendar days**.
- **"14 days"** is the **pre-2019** rule. Older sources and the onecle mirror still show it.
- **"28 days"** appears in a law-firm blog that describes the change as "fourteen days to twenty-eight days," while its own quotation of the statute says 30. **No statutory basis was found for 28.**
- Possible explanations, which are **hypotheses, not law**:
  - Some sheriffs charge a **$28 per-adjournment fee**: Gloucester County "Property Sale Adjournment: $28" ([fee page](https://gloucestercountynj.gov/563/Fees), effective Apr. 9, 2025, citing amended N.J.S.A. 22A:4-8); Atlantic County reportedly charges $28, or $56 for both.
  - Weekly sale calendars may produce 28-day (4-week) reschedules within the 30-day cap.

**County practice examples:**
- Gloucester County's **2026 Defendant Adjournment Form**: "the Sheriff may grant no more than two adjournments not exceeding 30 days"; further adjournments require a court order; the defendant may use both at once ([form](https://www.gloucestercountynj.gov/DocumentCenter/View/17252/2026-Defendant-Adjournment-Form)).
- Ocean County Sheriff quotes the 5-adjournment / 30-day text ([page](https://sheriff.co.ocean.nj.us/frmForeclosures)).
- Atlantic County applies the new rule to writs received on or after 7/29/2019 (secondary).

**Plaintiff adjournments:** the lender gets **2**, with **1** more joint. Before 2019, case law already read the statute as limiting debtor adjournments (*Bankers Trust Co. v. Delgado*, 346 N.J. Super. 103, 106-07 (App. Div. 2001); *Wells Fargo Home Mortg. v. Stull*, 378 N.J. Super. 449 (App. Div. 2005)), as cited in *Lazarus*.

**Court stays and "for cause":**
- Courts may order more adjournments for cause. Bankruptcy triggers the automatic stay (11 U.S.C. § 362).
- Court-ordered delay can **consume** the debtor's free adjournments. *Lazarus v. Tawil*, Docket F-4497-23 (Ch. Div. Monmouth, Aug. 1, 2025, not approved for publication) ([opinion](https://www.njcourts.gov/system/files/court-opinions/2025/lazarusvtawil--20250801.pdf)) held the debtors "implicitly enjoyed" their two free adjournments through earlier court stays and barred further without-cause adjournments, while still allowing applications for cause.

**Maximum statutory adjournment exposure:** 5 × 30 = **150 days** beyond the first scheduled date. The debtor's share alone is up to **60 days**.

### Typical total NJ foreclosure duration
- **ATTOM Q3 2023:** NJ average **1,593 days** from first public notice to completed foreclosure, the 5th-longest in the US (national average 770 days) ([ATTOM](https://www.attomdata.com/news/most-recent/attom-q3-and-september-2023-u-s-foreclosure-market-report/)). This is the most recent NJ-specific ATTOM day count found.
- **ATTOM Q3 2025 and Q4 2025:** NJ **not** in the top-5 longest states.
  - Q3 2025 5th place was Hawaii at **1,710** days; national average **608** days ([Q3 2025](https://www.attomdata.com/news/market-trends/foreclosures/q3-and-september-2025-foreclosure-market-report/)).
  - Q4 2025 5th place was Kansas at **1,594** days; national average **592** days.
  - In 2025 NJ had the **6th-highest** annual foreclosure rate (1 in 273 housing units) and the **highest** rate in December 2025 ([ATTOM year-end 2025, released Jan. 15, 2026](https://www.attomdata.com/news/most-recent/2025-year-end-foreclosure-market-report/)).
  - **VERIFY:** the exact NJ 2025 day count from ATTOM's state table.
- **Planning assumption:** roughly 3–4.5 years from first notice to completed foreclosure. Statutory minimums are much shorter (about 30-day NOI + 35-day answer + judgment + ≤150 days to sale + ≤150 days of adjournments + 10 days).

### What the software must do
1. Pre-foreclosure lead scoring: derive `NOI_date` (from DCA filings, if available) and flag `NOI_stale` if more than 180 days have passed without a complaint.
2. Sheriff-sale tracker:
   - Store `original_sale_date` and each adjournment `{requested_by: lender|debtor|joint|court, from, to}`.
   - **Validate** `to − from ≤ 30 calendar days` for statutory adjournments, counts ≤ 2 lender / 2 debtor / 1 joint, and that anything beyond is labelled `court-ordered`.
   - Never present "28 days" or "14 days" as the rule.
3. Script guardrail: callers must not tell owners they have "two 28-day adjournments" or advise on postponing a sale. Postponement advice is a "distressed property service" under FRFPA (§4).
4. Post-sale: display a **10-day redemption/objection window** from the sale date (60 days if a bankruptcy was filed). Do not count a sheriff-sale purchase as closed until the deed is delivered.
5. CWPP: show the posted upset price (≥ 4 weeks pre-sale). Note the owner/next-of-kin/tenant first-refusal right and the 3.5% / 90-business-day terms. Treat the nonprofit second-refusal right as legally unsettled (A-1098-25).

---

## 4. Consumer Fraud Act (N.J.S.A. 56:8-1 et seq.)

### Rule in plain English
Any **unconscionable or abusive** commercial practice, deception, fraud, false promise or misrepresentation, or knowing omission of a material fact, made "in connection with the **sale or advertisement of any merchandise or real estate**" is unlawful, **whether or not anyone was actually misled or damaged**. Private plaintiffs who prove an **ascertainable loss** caused by the violation get **mandatory treble damages plus attorneys' fees and costs**. The AG can impose civil penalties.

### Citations and numbers
- **N.J.S.A. 56:8-2**, as amended by [**P.L.2022, c.96**](https://pub.njleg.gov/bills/2022/PL22/96_.HTM) (A1556 1R, approved **Aug. 5, 2022**; §§1, 2 and 4 effective immediately). It added "commercial practice that is **unconscionable or abusive**."
- **N.J.S.A. 56:8-4(b)** (same act): in AG actions, **any commercial practice that violates State or federal law is conclusively presumed to be an unlawful practice** under 56:8-2. A TCPA, FRFPA or Daniel's Law violation is therefore automatically a CFA violation in an AG suit.
- **N.J.S.A. 56:8-19:** "the court shall … award **threefold the damages** sustained … [and] shall also award **reasonable attorneys' fees, filing fees and reasonable costs of suit**." ([Justia](https://law.justia.com/codes/new-jersey/title-56/section-56-8-19/); onecle mirror)
- **N.J.S.A. 56:8-20** (as amended 2022): a party asserting a CFA claim must e-mail pleadings to the AG **within 24 hours** of filing.
- **N.J.S.A. 56:8-13:** civil penalty up to **$10,000 first offense** and **$20,000 each subsequent offense** (onecle mirror). Enhanced penalties apply where victims are seniors or disabled (56:8-14.3; 56:8-1(f) defines "senior citizen" as **60+**).
- **Elements:** unlawful conduct, ascertainable loss, causal relationship. **VERIFY** pin cite: *Bosland v. Warnock Dodge, Inc.*, 197 N.J. 543 (2009).

### Application to "we buy houses" investors
- ***D'Agostino v. Maldonado*, 216 N.J. 168 (2013)** ([FindLaw](https://caselaw.findlaw.com/court/nj-supreme-court/1645937.html)) is directly on point:
  - **Facts:** an investor advertising **"I buy houses"** on his vehicle took title to distressed homeowners' $480,000 home for **$10** in a "foreclosure rescue" deal.
  - **Holding:** the Court held the CFA applies because he "offered 'services' for a fee to the public." The conduct was an **unconscionable commercial practice**.
  - **Damages:** the loss of equity was an **ascertainable loss** even though equity was later restored by court order. Treble damages (trial court: $150,694 net) and fees ($50,590) were reinstated.
  - **Left open (n.3):** whether this was a "sale of real estate" and whether a non-professional-seller exemption applies.
- **Takeaway:** a homeowner **selling to** a wholesaler can sue the wholesaler under the CFA. "Cash offer," "no fees," "close in 7 days," or "we'll save your credit/stop foreclosure" claims that are untrue or unconscionable expose the business to **treble damages plus fees**.

### Foreclosure Rescue Fraud Prevention Act (FRFPA): critical for pre-foreclosure leads
[**P.L.2011, c.146**](https://pub.njleg.gov/bills/2010/PL11/146_.HTM), **N.J.S.A. 46:10B-53 to -68** (text from the 2011 chapter law; **VERIFY** later amendments).
- **"Distressed property"** = residential **1–4 units**, **owner-occupied primary residence**, that is **in mortgage foreclosure** or whose owner is **> 90 days delinquent** on any loan secured by it.
- **"Distressed property purchaser"** = a person who acquires an interest through a **distressed property conveyance** or a **conditional conveyance** (a sale-leaseback or buy-back option), including joint-venture participants.
  - The **conveyance** definitions require "participation by, or any distressed property service … provided by, a **foreclosure consultant**."
  - **VERIFY with counsel:** whether a plain wholesaler purchase with no consultant involvement is covered. Marketing that promises to stop or postpone foreclosure, save credit or negotiate with the lender can make the business, or a partner, a "foreclosure consultant."
- **If it applies:**
  - **Contract form (46:10B-60):** written, **14-point bold**, in the language used to negotiate, fully completed, **signed and notarized** before any deed. Includes the purchaser's name, business address and phone, the property address, total consideration, and complete payment terms.
  - **Mandatory notices:** "NOTICE REQUIRED BY NEW JERSEY LAW … Until your right to cancel this contract has ended, [Name] … CANNOT ask you to sign or have you sign any deed or any other document. You are urged to have this contract reviewed by an attorney of your choice within 10 business days of signing it." If title transfers, also: "As part of this transaction, you are giving up title to your home."
  - **Cancellation (46:10B-61):** the owner may cancel **until midnight of the 10th business day** after signing (business day excludes Saturday, Sunday and **federal** holidays), or until the conclusion of the sheriff's sale, whichever is first.
    - The clock does **not start** until all parties sign and the purchaser has complied.
    - Cancellation may be delivered by any means; mail is effective on deposit.
    - The purchaser must return documents within **10 business days**.
    - The contract must include the cancellation statement and a detachable **NOTICE OF CANCELLATION** form in duplicate (46:10B-62).
  - **Recording (46:10B-62(d)):** record the contract and cancellation notice with the county clerk **within 10 business days**.
  - **Prohibitions until the cancellation period ends (46:10B-63(a)(6)):** do not accept, induce or record any deed or other document.
  - **Distressed property conveyance (46:10B-64):**
    - The owner must receive consideration of **at least 82% of fair market value**. An appraisal by a licensed appraiser creates a rebuttable presumption of FMV.
    - Documents must be notarized by an **unrelated** notary.
    - Closing must take place at a **licensed title producer's or NJ attorney's office**.
    - A **DOBI disclosure statement** of the owner's costs must be provided.
    - All **mortgage lienholders** must be notified in writing.
    - Due-on-sale and other mortgage terms must be complied with.
  - **Conditional conveyance (buy-back) extras:** verify ability to pay; buy-back option of **≥ 3 years**; fees ≤ **3.5%** of price; FMV ≥ **82%**; no power of attorney; record the deed with an FRFPA statement.
  - **Penalties (46:10B-67):**
    - Civil **$10,000 / $20,000**.
    - **Third-degree crime**, or **second-degree** for a pattern.
    - Owner may sue for **treble damages and attorneys' fees** and may file a lis pendens.
    - Conditional conveyances are voidable for **2 years**.
    - Waivers are void.
    - CFA remedies are preserved.

### Other NJ enforcement found
- **No** AG/Division of Consumer Affairs action specifically against a "wholesaler" or "we buy houses" company was found.
- Related actions found: foreclosure-rescue complaints (e.g., 2008, [JP Global complaint](https://www.nj.gov/oag/newsreleases08/102008-JP-Global-Complaint.pdf)), mortgage-modification scams, and the 2015 surplus-funds recovery settlements above.
- **VERIFY:** search the DCA press archive and court dockets periodically.

### What the software and scripts must do
1. **Claim substantiation:** every marketing claim ("cash," "no fees," "close in X days," "as-is," "we pay closing costs") must map to a contract term the business actually honors. Block publishing claims without a linked substantiation record.
2. **Prohibited script phrases:** "stop/postpone your foreclosure," "we'll negotiate with your lender," "save your credit," "we're here to help you (as your advisor)," or implying the buyer represents the seller (FRFPA 46:10B-63(a)(4)).
3. **Distressed flag:** if a lead is owner-occupied 1–4 units AND (in foreclosure OR more than 90 days delinquent), set `FRFPA_risk = true`. Then:
   - (a) route to attorney;
   - (b) use the FRFPA contract template (14-pt bold, notarized, notices, cancellation form);
   - (c) compute `cancel_deadline` = midnight of the 10th business day (federal holidays) after the last signature, or the sheriff sale, whichever is earlier;
   - (d) **block** deed signing or recording before `cancel_deadline`;
   - (e) **block offers below 82% of FMV**, so standard "70% ARV − repairs" MAO math is non-compliant here;
   - (f) require closing at a title producer or attorney office;
   - (g) generate lienholder notice letters.
4. **Senior flag:** sellers aged 60+ are subject to enhanced CFA penalties. Add a review step.
5. Log all scripts, call recordings and texts for **≥ 6 years**. That figure is the CFA/contract limitations period by analogy; **VERIFY** with counsel.

---

## 5. Wholesaling legality & licensing in NJ

### Rule in plain English
NJ has **no statute that specifically authorizes, defines or bans wholesaling** (as of Oct. 26, 2026). The risk comes from the broker-licensing law. Anyone who, **for a fee or other valuable consideration**, "lists for sale, sells, … buys … or offers or attempts to negotiate a sale … of real estate **or an interest therein**," or solicits buyers, is acting as a broker and must be licensed. Unlicensed persons **cannot sue** to collect compensation for those acts. A **"bona fide owner"** acting on property it owns is exempt. Whether a mere contract purchaser assigning its equitable interest counts as a "bona fide owner" is **not settled** by any NJ authority found.

### Citations
- **N.J.S.A. 45:15-1:** no person may engage, "directly or indirectly," in the business of a broker/salesperson, or **advertise** or represent themselves as authorized to do the acts in 45:15-3, without a license. ([Justia](https://law.justia.com/codes/new-jersey/title-45/section-45-15-1/); onecle mirror; last amended 2009, c.238)
- **N.J.S.A. 45:15-3:** broker definition, quoted above. "No person … shall bring or maintain any action in the courts of this State for the collection of compensation for the performance of any of the acts mentioned … without alleging and proving that he was a duly licensed real estate broker at the time the alleged cause of action arose." (onecle mirror)
- **N.J.S.A. 45:15-4:** the article does not apply to a person "who, as a **bona fide owner** or lessor, shall perform any of the aforesaid acts with reference to property owned by him," nor to attorneys, receivers and court-ordered sellers. (onecle mirror)
- **N.J.S.A. 45:15-17:** the Commission may investigate **any person** acting without a license. Penalty **≤ $5,000 first violation, ≤ $10,000 each subsequent**, and "**each transaction shall be construed as a separate offense**." (onecle mirror) REC consent orders show unlicensed-activity fines of about $1,000–$5,000 ([REC discipline 2016–2026](https://www.nj.gov/dobi/division_rec/recdiscp2025.htm)).
- **N.J.S.A. 45:15-16:** salespersons may accept compensation only from their employing broker. Licensees cannot split fees with unlicensed wholesalers.

### NJ Real Estate Commission statements
- **None found.** No REC bulletin, advisory or rule on wholesaling or marketing equitable interests was located.
- A scan of the REC public disciplinary-action pages for **2019–2026** for "wholesal," "equitable interest" and "assignment of contract" returned **0 hits**.
- **VERIFY:** ask the REC (DOBI) directly.

### NJ wholesaling bills (Legislature search)
| Bill | Session | Status | Key provisions |
|---|---|---|---|
| **A4278** (Hutchison, Miller) ([text](https://pub.njleg.gov/bills/2026/A4500/4278_I1.HTM)) | 2026–27 (222nd Leg.), introduced **Feb. 19, 2026** | Referred to Assembly Regulated Professions Committee; **pending** (no further action found) | Defines "**wholesale transaction**" as promoting the sale, exchange or purchase of an **equitable or other interest** in 1–4 unit residential property, intending to assign or transfer it for a fee **without taking title**. **Requires a broker/salesperson license.** Consumer may cancel until **midnight of the 30th day** after signing or until conveyance (whichever first). If disclosures are missing, may cancel any time before conveyance. Right is non-waivable. Cancellation by certified mail or any bona fide method with receipt. **Refund within 10 business days**. Mandatory contract disclosures: wholesale nature, right to an appraisal, right to consult an unaffiliated licensee or counsel, the cancellation right, the refund. Effective the **1st day of the 4th month** after enactment. |
| **A3317** (McKnight) ([text](https://pub.njleg.gov/bills/2022/A3500/3317_I1.HTM)), introduced Mar. 7, 2022 / **S3824** (Cryan) ([text](https://pub.njleg.gov/Bills/2022/S4000/3824_I1.HTM)), introduced May 11, 2023 | 2022–23 (220th) | **Died** at session end | "Residential property wholesaler" license from the REC ($200 application fee; insurance; annual renewal; 6-year fraud-conviction bar). Bad-faith solicitation banned. Written disclosure to the homeowner **≥ 3 days before presenting an offer** (FMV resources, etc.). |
| 2024–25 (221st) | | **None found**. **VERIFY** with a full-text "wholesal" search on njleg.state.nj.us | |

### Safe operating structure (risk-ranked; confirm with NJ counsel)
1. **Lowest risk:**
   - **Take title then resell** (double close or fund-and-flip). The business is then a **bona fide owner** under 45:15-4.
   - Pay the realty transfer fee on each deed as grantor (§8).
   - Comply with the CFA and FRFPA on acquisition.
2. **License or broker partnership:**
   - The principal, or a disposition partner, holds an NJ broker or salesperson license, with properly supervised compensation (45:15-16).
   - Attorney review then applies to licensee-prepared contracts (§1).
   - A4278, if enacted, would make licensure **mandatory** for assignments.
3. **Assignment of contract (higher risk):**
   - Sign as **principal buyer** with real intent and ability to close.
   - Disclose in the contract the **right to assign** and the intent to profit by assignment or resale.
   - Market **only the contract position** to a private buyer list. Do not advertise the property as "for sale" or imply ownership. Do not take fees from the seller.
   - Unresolved exposure:
     - 45:15-3 covers selling "an interest therein" for consideration.
     - 45:15-3 bars suing to collect an assignment fee if the end-buyer refuses.
     - 45:15-17 fines apply per transaction.
     - CFA misrepresentation claims.
4. **Never:** "list" a property you don't own, take an "option" plus marketing in exchange for a fee from the seller, or co-broker with licensees for a split.

### What the software must do
1. Track `deal_structure ∈ {double_close, assignment, licensed_dispo, JV}` with **assignment flagged "elevated licensing risk – NJ"** and a counsel sign-off field.
2. Dispo marketing module: watermark materials "Assignment of contract interest – seller is not the record owner"; prohibit MLS or public listing for `assignment` deals; prohibit "for sale by owner" language.
3. Hold assignment-fee invoices until the assignment agreement is fully signed. The fee may be **uncollectible in court** (45:15-3), so require the fee in escrow at closing.
4. Legislative watch: a **feature flag for A4278**. If enacted, enable a 30-day consumer cancellation timer, license checks, mandatory disclosures and 10-business-day refunds, effective on the first day of the 4th month after enactment.

---

## 6. Telemarketing

### 6.1 TCPA basics as of Oct. 2026
- **Statute and rules:** 47 U.S.C. § 227; **47 C.F.R. § 64.1200** ([eCFR, current](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-B/part-64/subpart-L/section-64.1200); text pulled as of 2026-10-01).
- **Autodialer (ATDS):** equipment that can "store or produce telephone numbers to be called **using a random or sequential number generator**" (64.1200(f)(2)). ***Facebook, Inc. v. Duguid*, 592 U.S. 395 (2021)** limited ATDS to that RNG capability. Most CRM and click-to-dial tools are not ATDS. **Prerecorded and artificial-voice rules apply regardless of dialer type.**
- **Texts are "calls":** *Campbell-Ewald Co. v. Gomez*, 577 U.S. 153 (2016). The FCC applies the DNC rules to texts (64.1200(e)).
- **Prerecorded / artificial voice (incl. AI voice and ringless voicemail):**
  - **To cell phones (64.1200(a)(1)(iii)):** any call using an ATDS **or** an artificial/prerecorded voice needs the called party's **prior express consent**. Telemarketing or advertising content needs **prior express written consent** (64.1200(a)(2)).
  - **To residential landlines (64.1200(a)(3)):** telemarketing needs **prior express written consent**. Non-telemarketing commercial prerecorded calls are limited to **3 calls per 30 days** with opt-out.
  - **AI-generated voices are "artificial"** under the TCPA: FCC Declaratory Ruling, **FCC 24-17**, CG Docket 23-362 (Feb. 8, 2024) ([summary](https://wsgr.com/en/insights/fcc-rules-ai-generated-voices-are-artificial-under-the-tcpa.html)).
  - **Ringless voicemail = a "call"** using a prerecorded voice, so consent is required: **FCC 22-85**, CG Docket 02-278 (adopted Nov. 14, released Nov. 21, 2022) ([order](https://docs.fcc.gov/public/attachments/FCC-22-85A1_Rcd.pdf); [Fed. Reg.](https://www.govinfo.gov/content/pkg/FR-2022-12-14/html/2022-26673.htm)).
  - **Caveat (VERIFY):** after *McLaughlin Chiropractic Assocs. v. McKesson Corp.* (U.S. June 20, 2025), district courts are not bound by FCC interpretations. Treat the FCC rulings as persuasive and comply anyway.
- **National DNC (64.1200(c)(2)):**
  - No "telephone solicitation" to a residential subscriber on the registry.
  - **Safe harbor** requires written procedures, training, an internal list, and use of a registry version obtained **no more than 31 days** before the call.
  - Registrations are honored **indefinitely**.
- **Calling hours (64.1200(c)(1)):** no telephone solicitation **before 8 a.m. or after 9 p.m.** local time at the **called party's** location.
- **Company-specific DNC (64.1200(d)):**
  - Honor requests **within 10 business days** (d)(3) and **for 5 years** (d)(6).
  - Written policy, training and identification of caller and entity are required.
- **Revocation of consent (64.1200(a)(10)–(12), effective Apr. 11, 2025):**
  - Revocation by **any reasonable means**. "stop," "quit," "end," "revoke," "opt out," "cancel" and "unsubscribe" are per-se valid.
  - Honor within **10 business days**.
  - One confirmation text is allowed.
  - The "**revoke-all**" piece (one revocation covers unrelated message types) is **delayed to Jan. 31, 2027** (FCC CGB order, Jan. 6, 2026; [summary](https://www.burr.com/telephone-consumer-protection-act/the-fcc-delays-effective-date-of-tcpa-revoke-all-rule-until-january-31-2027)). **VERIFY** any later change.
- **One-to-one consent rule:** vacated by the 11th Circuit (*Insurance Marketing Coalition v. FCC*, Jan. 24, 2025) and never took effect. **VERIFY** citation.
- **Damages:**
  - **$500 per violation**, trebled to **$1,500** if willful or knowing (47 U.S.C. § 227(b)(3), (c)(5)).
  - The DNC private right requires **more than one call within 12 months** (§ 227(c)(5)).

### 6.2 Are offers to BUY a home "telephone solicitations"?
**Definition:** "the initiation of a telephone call or message for the purpose of encouraging the purchase or rental of, or investment in, property, goods, or services" (47 U.S.C. § 227(a)(4); 64.1200(f)(15)). Courts are **split**, and the trend favors plaintiffs where any **dual purpose** exists.

**Pure offers to buy are not solicitations:**
- *Coffey v. Fast Easy Offer, LLC*, No. 2:24-cv-02725 (D. Ariz. 2025), dismissing the claim. Reversed; see below.
- *Aussieker v. Aghazadeh*, 2025 WL 2021040 (E.D. Cal. July 18, 2025).
- *Hunsinger v. Offer, LLC* (N.D. Tex.), reported. **VERIFY** citation. The case is sometimes confused with *Hunsinger v. Alpha Cash Buyers*; the Alpha Cash opinions found do not decide this issue.

**Dual purpose can make them solicitations:**
- ***Coffey v. Fast Easy Offer, LLC*, No. 25-4066 (9th Cir. June 4, 2026) (published)** ([opinion](https://cdn.ca9.uscourts.gov/datastore/opinions/2026/06/04/25-4066.pdf)).
  - The relevant "purpose" is the purpose of initiating the call. An explicit mention of a product is not required (*Chesbro*).
  - Texts from a "home buyer" persona were plausibly telephone solicitations because one purpose was to generate **real-estate brokerage** leads (Keller Williams affiliates). The dismissal was reversed.
- *N.D. Ga.* (Mar. 4, 2026; [Orrick summary](https://infobytes.orrick.com/2026-03-13/judge-allows-some-consumer-protection-claims-over-unsolicited-home-buying-texts-to-proceed/)): claims that home-buying texts implicitly offered **bundled real-estate services** survived dismissal. Case name **VERIFY**.

**Software implication:** a wholesaler's "we buy houses" outreach is safest treated as **telephone solicitation**, especially if leads not purchased are referred to agents or partners, or if the business offers listing, novation or "creative" services. Prerecorded, AI or ringless messages to cell phones are a **separate § 227(b) violation without consent, regardless of solicitation status**.

### 6.3 DNC registry fees
Per the [FTC, Aug. 26, 2026](https://www.ftc.gov/news-events/news/press-releases/2026/08/ftc-announces-2027-telemarketer-fees-access-national-do-not-call-registry):

| | FY2026 (Oct. 1, 2025 – Sep. 30, 2026) | **FY2027 (from Oct. 1, 2026; current)** |
|---|---|---|
| Per area code | $82 | **$85** |
| Half-year additional area code | $41 | **$43** |
| Nationwide maximum | $22,626 | **$23,425** |
| First 5 area codes | Free | **Free** |

NJ has 10 area codes including overlays (201, 551, 609, 640, 732, 848, 856, 862, 908, 973). That list is from general knowledge; **VERIFY**. Statewide coverage therefore needs more than 5 codes: about 5 × $85 = $425 per year beyond the free 5 (FY2027).

### 6.4 New Jersey telemarketing law (N.J.S.A. 56:8-119 to -135)
Source: [P.L.2003, c.76](https://pub.njleg.gov/bills/2002/PL03/76_.HTM), as amended.
- **Scope:** a "Telemarketing sales call" is a call "to encourage the purchase or rental of, or investment in, **merchandise**." Merchandise is defined in the CFA as including "services or anything offered … to the public for sale." *D'Agostino* treated an "I buy houses" operator as offering **services**, which supports NJ coverage of "we buy houses" calls. **Treat as covered.**
- **Registration (56:8-121, -122):**
  - Every telemarketer, including out-of-state ones, must **register annually** with the **Division of Consumer Affairs** before making unsolicited telemarketing sales calls to NJ customers.
  - A criminal-history disclosure statement is required, and material changes must be reported within **30 business days** (56:8-125).
  - The Division may require a surety bond of at least **$25,000** (56:8-126).
  - Fees under **N.J.A.C. 13:45D-1.4**, by simultaneous outgoing call capacity: **$150** (1–5 lines), **$500** (6–15), **$2,000** (16+) (secondary source). **VERIFY** current fees on njconsumeraffairs.gov.
- **No-call list:** the state list is the **National DNC Registry** under N.J.A.C. 13:45D (secondary). A grace period applies only within **3 months** of the number's listing if the caller's list was under 3 months old (56:8-128(a) as amended by P.L.2023, c.58).
- **Disclosures ([P.L.2023, c.58](https://pub.njleg.gov/bills/2022/PL23/58_.HTM), "Seinfeld bill," approved May 15, 2023, effective Dec. 1, 2023):**
  - Within the **first 30 seconds**, accurately identify the telemarketer's name, the **name and telephone number** of the entity on whose behalf the call is made, and the **purpose** of the call. A violation is also a **disorderly persons offense**.
  - Disclose the **mailing address** on the telemarketer's website and in any later written communication.
  - No calls **9:00 p.m.–8:00 a.m.** customer local time.
  - **No caller-ID blocking or spoofing**, including numbers or names not associated with the telemarketer.
  - Exemption: calls the customer affirmatively requested via a phone call or website contact.
- **Cell phones (56:8-130):** limits unsolicited telemarketing sales calls to mobile devices. A 2015 amendment (S1382) reportedly allows them with an existing customer relationship or **express written consent** ([DWT summary](https://dwt.com/blogs/privacy--security-law-blog/2015/02/new-jersey-amends-do-not-call-law-eases-prohibitio)). **VERIFY** the chapter number and current text.
- **Penalties (56:8-132):** CFA penalties (**$10,000 / $20,000**, plus senior enhancements).
  - **Defense** available only if **all** apply: the no-call list is obtained and updated **quarterly**, written policies exist, staff are trained, records are kept, **and** the call was isolated (no more than once in 12 months).

### What the software must do
1. **Consent ledger** per phone number: `{consent_type: none|express|express_written, source, timestamp, disclosure_text, revoked_at}`. Block prerecorded, AI-voice and RVM sends to **any cell number** without consent. Block any prerecorded telemarketing to landlines without written consent.
2. **DNC scrub:**
   - Pull the national registry **≤ 31 days** old.
   - Scrub before every campaign; block registry numbers absent written consent or an established business relationship.
   - Maintain an internal DNC with **10-business-day** max honor time (implement as immediate) and **5-year** retention.
   - Apply DNC to SMS.
3. **Revocation parser:** treat STOP/QUIT/END/REVOKE/OPT OUT/CANCEL/UNSUBSCRIBE and reasonable equivalents as revocation. Send one confirmation text with no marketing. Apply within **10 business days**; implement as **immediate**. Prepare for **revoke-all** on **Jan. 31, 2027**.
4. **Calling window:** only **8:00 a.m.–9:00 p.m.** in the recipient's local time zone (use the property or phone-number time zone, whichever is more restrictive).
5. **Script opening (first 30 s):** caller name + company legal name + **company callback number** + purpose ("I'm calling about buying your property at ___"). Website footer and follow-up mail show the **mailing address**. Caller ID shows a number **owned by the company**.
6. **NJ registration gate:** block outbound campaigns to NJ numbers unless `nj_telemarketer_registration.valid_through ≥ today`.
7. Do not refer unconverted seller leads to agents or brokers for compensation without counsel review (*Coffey* dual-purpose risk).
8. Budget DNC area codes: ≥ 5 free; $85 each additional (FY2027).

---

## 7. Daniel's Law

### Rule in plain English
After a business receives **written notice** from a "covered person," or an authorized person or assignee such as **Atlas Data Privacy Corp.**, it must stop **disclosing or re-disclosing** that person's **home address or unpublished home phone number** within **10 business days**. "Disclose" is defined very broadly. It includes selling, giving, providing, trading, **mailing**, **transferring**, posting or publishing, and "making available or viewable within a searchable list or database." Liability for actual and liquidated damages is **strict**: no mental state is required.

### Citations
- **N.J.S.A. 56:8-166.1**, as amended by **P.L.2020, c.125** (Daniel's Law, Nov. 2020), **P.L.2021, c.371**, and [**P.L.2023, c.113**](https://pub.njleg.gov/bills/2022/PL23/113_.HTM) (approved July 20, 2023; §6 effective immediately).
  - **(a)(1) Deadline:** "not later than **10 business days** following receipt" of the notice, a person or business "shall not disclose or re-disclose on the Internet or otherwise make available" the protected information.
  - **(a)(3)** A family member who moves out must notify within 30 days.
  - **Covered persons:** active, former or retired **judicial officers, law enforcement officers, prosecutors, child protective investigators (DCPP)**, and immediate family members residing in the same household. "Home telephone number" includes **cell phones**.
  - **(c) Remedies (court "shall award"):**
    - **actual damages, not less than liquidated damages of $1,000 per violation**;
    - **punitive damages** on proof of willful or reckless disregard;
    - **reasonable attorneys' fees and costs**;
    - injunctive relief.
  - The claim may be brought by the covered person or an **assignee** (Atlas holds thousands of assignments).
- **Government records:** N.J.S.A. 47:1A-1.1 (OPRA definitions, amended by c.113) and **N.J.S.A. 47:1B-1 to -3** (P.L.2021, c.371) provide address redaction by public agencies through the DCA Office of Information Privacy. A business that received unredacted data under 47:1B may use it only for the purposes for which it was received (56:8-166.1(e)).
- **Criminal:** **N.J.S.A. 2C:20-31.1**, as described in *Atlas* (2026): **reckless** disclosure is a **4th-degree** crime and **purposeful** disclosure is a **3rd-degree** crime. **VERIFY** current text.

### Atlas Data Privacy litigation status (as of Oct. 26, 2026)
- **D.N.J.:** Judge Bartle denied motions to dismiss and held Daniel's Law facially constitutional, reading in a negligence standard (*Atlas Data Privacy Corp. v. We Inform, LLC*, 758 F. Supp. 3d 322 (D.N.J. 2024)).
- **3d Cir. No. 25-1555** (consolidated appeals; argued **July 8, 2025**): the Third Circuit certified the mental-state question to the NJ Supreme Court.
- **NJ Supreme Court, *Atlas Data Privacy Corp. v. We Inform, LLC*, A-8-25 (decided Aug. 12, 2026, unanimous)** ([opinion](https://law.justia.com/cases/new-jersey/supreme-court/2026/a-8-25.html)):
  - "Daniel's Law does **not** contain a mental state requirement for actual damages liability under N.J.S.A. 56:8-166.1(c)(1)."
  - Constitutionality was left to the Third Circuit.
  - Defendants in the consolidated cases include **"real estate businesses, and direct-mailing and/or marketing companies."**
- **Third Circuit decision on constitutionality:** **not found** as of this date. **VERIFY** the docket.

### What the software must do
1. **Intake channel:**
   - A monitored e-mail/address for Daniel's Law notices. Atlas notices often arrive in bulk by e-mail.
   - Auto-acknowledge.
   - Parse the name, address and phone.
   - Start an SLA timer of **10 business days**. Count holidays as business days so the deadline errs short; target **same-day** suppression.
2. **Suppression:** mark matching owner records `daniels_law_protected = true`. **Block** any export, list sale or share, buyer-list post, skip-trace upload, direct-mail file transfer to a vendor, public deal page, or API exposure of the **address or phone**. Do not just hide it in the UI; data sharing with mail houses, skip tracers and dispo partners is "disclose."
3. Keep internal records only as needed, with an audit log of notice receipt, suppression time and propagation to vendors. Require vendors (mail house, SMS, skip trace, list brokers) to honor suppression within the same window, by contract.
4. Mail campaigns: suppress protected addresses from **outbound mailing lists sent to third-party printers**. "Mail" and "transfer" are enumerated acts. **VERIFY** with counsel whether mailing *to* the covered person's own address is a "disclosure." Atlas has sued direct-mail and real-estate companies.
5. Budget risk at **$1,000 per violation per person**, plus fees, with **strict** liability.

---

## 8. NJ realty transfer fee & "mansion tax" (Graduated Percent Fee)

### Seller-paid base realty transfer fee (RTF)
The base RTF is paid by the **grantor (seller)** when the deed is recorded. Components:
- **Basic fee:** $1.25 State + $0.50 county = $1.75 per $500 (N.J.S.A. 46:15-7(a)(1)).
- **Additional fee:** $0.75 per $500 above $150,000 (46:15-7(a)(2)).
- **General purpose fee** when total consideration exceeds $350,000 (46:15-7(a)(3)): $0.90 up to $550,000, $1.40 for $550,000–$850,000, $1.90 for $850,000–$1,000,000, and $2.15 above $1,000,000.
- **Supplemental fee:** $0.25 up to $150,000, $0.85 for $150,000–$200,000, $1.40 above $200,000 (46:15-7.1).

The statute text came from the onecle mirror. Summed brackets match the Morris County Clerk schedule ([page](https://morriscountyclerk.org/Registry/Fees)), except that Morris lists "$4.255", which appears to be a typo for $4.25.

**Rate per $500 of consideration (or fraction), applied marginally by bracket:**

| Bracket | Total consideration ≤ $350,000 | Total consideration > $350,000 | Partial exemption (senior 62+/blind/disabled/low-moderate income), ≤ $350k | Partial exemption, > $350k |
|---|---|---|---|---|
| $0 – $150,000 | **$2.00** | **$2.90** | $0.50 | $1.40 |
| $150,000.01 – $200,000 | **$3.35** | **$4.25** | $1.25 | $2.15 |
| $200,000.01 – $350,000 | **$3.90** | **$4.80** (to $550k) | $1.25 | $2.15 |
| $550,000.01 – $850,000 | — | **$5.30** | — | $2.65 |
| $850,000.01 – $1,000,000 | — | **$5.80** | — | $3.15 |
| > $1,000,000 | — | **$6.05** | — | $3.40 |

- **New construction** uses the full rates (2004 Treasury schedules: [≤ $350k](https://nj.gov/treasury/taxation/pdf/lpt/feeschedunder350.pdf), [> $350k](https://nj.gov/treasury/taxation/pdf/lpt/feeschedover350.pdf)).
- **Exemptions** (46:15-10) include consideration under $100, **sales for delinquent taxes**, deeds "in specific performance of a final judgment," spouse/parent-child transfers, etc.
- **VERIFY:** the age threshold (62+) for the senior partial exemption, and whether the partial exemption still exempts the full general-purpose fee.

### "Mansion tax", now the Graduated Percent Fee (FY2026 budget change)
- **P.L.2025, c.69**, enacted **June 30, 2025**, **effective July 10, 2025**, amended **N.J.S.A. 46:15-7.2**. Per NJ Division of Taxation memos of **July 2, 2025** ([notice](https://nj.gov/treasury/taxation/pdf/lpt/GraduatedPercentFeeNotice.pdf)) and **July 9, 2025** ([policies](https://nj.gov/treasury/taxation/pdf/other_forms/lpt/GraduatedPercentFeePolicies.pdf)):
  - The former **1% buyer-paid** fee on transfers over $1 million became a **Graduated Percent Fee** legally imposed on the **grantor (seller)**.
  - It is a **flat percentage of the TOTAL consideration**, not marginal:
    - **1%** if > $1,000,000 and ≤ $2,000,000
    - **2%** if > $2,000,000 and ≤ $2,500,000
    - **2.5%** if > $2,500,000 and ≤ $3,000,000
    - **3%** if > $3,000,000 and ≤ $3,500,000
    - **3.5%** if > $3,500,000
  - Treasury example: a $2.75M deed owes 2.5% × $2.75M = **$68,750**, plus other grantor fees.
  - **Operative date** = the date the deed is **submitted** for recording. Deeds received (or postmarked) on or before **July 9, 2025** paid the old 1%.
  - **Transition refund:** if consideration exceeds $2M, the contract was fully executed **before July 10, 2025**, and the deed was recorded by **Nov. 15, 2025**, the seller could claim a refund of the amount above 1% within 1 year (Form RTF-3).
  - The parties may shift the economic burden **by contract**, but legal liability and refund rights stay with the grantor.
  - No senior/disabled/veteran discount on the GPF (per NJ Realtors).
- **Property classes covered:** historically the 1% fee applied to Class 2 residential, Class 3A farm with residence, cooperative units, and Class 4A commercial over $1M. **VERIFY:** whether c.69 changed class coverage, and its parallel changes to the **Controlling Interest Transfer Tax** (secondary sources say graduated rates also apply).
- **Combined top-tier rate** for a deed over $3.5M: about 1.21% RTF + 3.5% GPF = **about 4.71%** (EY summary).

### Related closing items
- Non-resident seller estimated income tax (GIT/REP): Morris County lists **2%** of consideration. Treasury rule is the greater of 2% of consideration or the top rate on gain. **VERIFY.**

### What the software must do
1. Deal P&L: compute RTF with the **two-schedule** logic. The total consideration ≤ $350,000 vs > $350,000 decides which schedule applies to **all** brackets. Round consideration **up** to each $500 increment ("or fractional part thereof").
2. For each deed in a **double close**, charge RTF to the grantor of **that** deed: the A→B deed paid by the homeowner (or credited by contract), and the B→C deed paid by the wholesaler. An **assignment** has only one deed.
3. If consideration exceeds $1,000,000 and the deed is submitted on or after 2025-07-10, add the **GPF to the seller side** at the flat tier rate. Never put the old 1% on the buyer unless a contract shifts it.
4. Flag the partial-exemption eligibility questions (senior/disabled/low-income seller) for the closing attorney.
5. Keep rate tables versioned with effective dates (2004/2006 schedule; 2025-07-10 GPF).

---

## 9. Rules the software must enforce (checklist)

**Attorney review**
- [ ] Licensee-prepared contract for 1–4 family or vacant one-family lot → mandatory header + clause (N.J.A.C. 11:5-6.2(g)); **3 NJ business days** from delivery to **both** parties; exclude Saturdays, Sundays and N.J.S.A. 36:1-1 holidays.
- [ ] Accept disapproval by fax / e-mail / personal delivery / overnight mail (effective on mailing) / certified mail / telegram (*Conley*, 2017).
- [ ] No "binding" status, dispo marketing or paid third-party orders until review ends.

**Tax liens**
- [ ] Bid rate **0–18%**; premium only at below 1%.
- [ ] Penalty **2% (> $200) / 4% (> $5,000) / 6% (> $10,000)** of the certificate amount, on **redemption only**.
- [ ] Subsequent taxes earn the **municipal delinquent rate (≤ 8% to $1,500 / ≤ 18% above)**; year-end penalty ≤ **6%** on delinquency over **$10,000**.
- [ ] Foreclosure filing **≥ 6 months** (municipal) / **≥ 2 years** (third party) after sale; anytime if certified abandoned.
- [ ] Redemption allowed **until final judgment**; no fixed "redemption period expiry."
- [ ] Post-July 10, 2024: owner may demand **sheriff sale/Internet auction before final judgment**; holder surplus lien **10% capped at $5,000**; reopen judgment only within **3 months** (jurisdiction/fraud).
- [ ] Pre-complaint notice **≥ 30 days**, certified mail; costs capped (**$350** search / **$150** attorney pre-complaint; **$2,500** attorneys' fee after filing).
- [ ] Premium refund clock **5 years** (writ to sheriff within 5 years in the sale path).

**Mortgage foreclosure / sheriff sales**
- [ ] NOI **30–180 days** before filing; re-send if more than 180 days pass.
- [ ] Answer **35 days**; mediation initiation window **≥ 60 days**.
- [ ] Sheriff sale within **150 days** of receiving the writ.
- [ ] Publication **4 consecutive weeks** (first ≥ 21 days, last ≤ 8 days before); posting ≥ **3 weeks**; mailed notice ≥ **10 days**; upset price posted ≥ **4 weeks**, max **+3%**.
- [ ] Adjournments: **2 debtor + 2 lender + 1 joint, each ≤ 30 calendar days** (post-7/28/2019); more only by court order.
- [ ] Deposit **20%**; CWPP-qualified bidders **3.5%** with balance in **90 business days**.
- [ ] Post-sale **10-day** redemption/objection window (**60 days** if bankruptcy).

**Consumer protection**
- [ ] Every marketing claim substantiated; banned phrases list (CFA / FRFPA).
- [ ] FRFPA flag (owner-occupied 1–4 unit; in foreclosure or more than **90 days** delinquent). If set: 14-pt bold notarized contract; **10-business-day** cancellation; no deed until it expires; record contract within **10 business days**; **≥ 82% FMV**; title/attorney-office closing; lienholder notices.
- [ ] CFA exposure: treble damages + fees; AG penalties **$10,000 / $20,000**; any law violation is a per-se CFA violation in AG suits.

**Licensing**
- [ ] Assignment deals flagged as licensing risk; no public listing of non-owned property; prefer double close or licensed dispo.
- [ ] Watch A4278: **30-day** cancellation, **10-business-day** refund, license required.

**Telemarketing**
- [ ] Calls and texts only **8 a.m.–9 p.m.** recipient local time.
- [ ] National DNC data **≤ 31 days** old; internal DNC honored in **≤ 10 business days**, kept **5 years**.
- [ ] Consent required for prerecorded/AI/RVM to cells. Opt-out keywords honored in **≤ 10 business days** (target immediate). Prepare for revoke-all on **Jan 31, 2027**.
- [ ] NJ: annual DCA telemarketer registration before calling NJ numbers; ID name + company + **company phone** + purpose within **30 s**; mailing address on website; no spoofing.
- [ ] DNC fees FY2027: **$85** per area code after the **first 5 free**; max **$23,425**.

**Daniel's Law**
- [ ] Suppress disclosure of covered persons' address/phone within **10 business days** of notice (target same day), including vendor transfers and mail files.
- [ ] Liability **$1,000 minimum per violation**, strict liability (NJ S. Ct. Aug. 12, 2026).

**Transfer fees**
- [ ] RTF paid by grantor using the two-schedule table (≤ $350k vs > $350k), per $500 or fraction.
- [ ] GPF on the **seller** for deeds over **$1M** submitted on or after **2025-07-10**: **1 / 2 / 2.5 / 3 / 3.5%** of total consideration.

---

## 10. Corrections

| Claim in the brief | Verdict | Correct statement |
|---|---|---|
| "Homeowner has a statutory right to **two 28-day** sheriff sale adjournments" | **Wrong (number); partly right (count)** | Under **N.J.S.A. 2A:17-36** as amended by **P.L.2019, c.71** (effective **July 28, 2019**), the **debtor** may obtain **two** adjournments, each **"not exceeding 30 calendar days."** The lender gets two, and one more is available if both agree, for **five total**. Further adjournments only by court order "for cause." **14 days** was the pre-2019 rule. **No source supports 28 days**; it may come from a blog error, 4-week sale cycles, or the **$28** adjournment fee some sheriffs charge. The right is not absolute in practice: court-ordered stays can be treated as using it up (*Lazarus v. Tawil*, Ch. Div. 2025). Bankruptcy and court stays operate separately. |
| "**18% interest rate cap**" | **Correct but incomplete / misleading as a yield cap** | 18% is the **maximum bid interest rate** on the certificate (N.J.S.A. 54:5-32). Municipal default certificates are at 18% (54:5-34). Competitive bidding drives it down, often to **0% + premium**, and the premium earns **no interest**. The holder's total return can exceed 18% annualized because of: the **2%/4%/6% redemption penalty** (54:5-61); interest on **subsequent taxes at the municipal delinquent rate** (≤ 8% / ≤ 18%) and a year-end penalty ≤ **6%** (54:4-67); and recoverable costs and fees (54:5-97.1, 54:5-98). The cap does **not** limit surplus equity: after *Tyler*, *Roberto* and P.L.2024, c.39, holders may recover only the debt, interest and costs, never the owner's surplus equity. |
| "**2-year redemption period** for third-party lienholders" | **Wrong framing** | NJ has **no fixed redemption period that expires**. The owner may redeem **at any time until a final judgment** bars redemption (N.J.S.A. 54:5-54, 54:5-86). **2 years** is the **minimum waiting period before a third-party holder may file** a foreclosure action (**6 months** for a municipality or its assignee; **any time** for certified abandoned property). Redemption continues after filing until final judgment, and the judgment can be reopened only within **3 months** for lack of jurisdiction or fraud (54:5-87). Since July 10, 2024, owners may also demand a sheriff sale or Internet auction before judgment to preserve equity. |
| (Implicit) "attorney review **1984** modification" | **Minor** | The NJ Supreme Court cites the modification as **94 N.J. 449 (1983)** (*Conley*, 2017; *Sullivan*, 2022). |
| (Implicit) Attorney-review disapproval "by certified mail/telegram/personal delivery" only | **Outdated** | *Conley v. Guerrero* (2017) also allows **fax, e-mail, and overnight mail** (effective on mailing). |

---

## 11. Open VERIFY items
1. Current official text of N.J.S.A. 54:5-58/-60/-61 and 54:4-67. The mirror versions date from 2016 or earlier; confirm no post-2016 amendments, especially penalty tiers and the treatment of premium certificates.
2. The R. 4:6-1, R. 4:64-1, R. 4:64-3, R. 4:65-2 and R. 4:65-5 texts against the official NJ Court Rules (the njcourts.gov rules pages could not be fetched).
3. Whether P.L.2019, c.134 (filing the NOI with DCA) and any 2024–2026 Fair Foreclosure Act amendments add NOI content.
4. The NJ-specific ATTOM 2025 average days to foreclose.
5. FRFPA applicability to plain wholesaler purchases without a "foreclosure consultant," and any amendments since 2011.
6. Whether a contract vendee or assignor is a "bona fide owner" under N.J.S.A. 45:15-4. Also any REC guidance on wholesaling, and 2024–25 (221st) wholesaling bills.
7. Current N.J.A.C. 13:45D telemarketer registration fees and no-call rules; the chapter number of the 2015 cell-phone amendment (S1382).
8. Citations for *Hunsinger v. Offer, LLC*; the N.D. Ga. 2026 case name; *Insurance Marketing Coalition v. FCC*; *McLaughlin Chiropractic v. McKesson*.
9. Any Third Circuit decision in *Atlas* (No. 25-1555) after Aug. 12, 2026; the current text of N.J.S.A. 2C:20-31.1.
10. N.J.S.A. 36:1-1 holiday list (Juneteenth and others) for business-day calendars.
11. P.L.2025, c.69's effect on property classes and the Controlling Interest Transfer Tax; senior partial-exemption details for the RTF.
12. Status of the CWPP 2A:50-64(g) ruling (A-1098-25): further review or legislative amendments (S4470/A5702).
