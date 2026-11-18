# Meridian: Report Calculation and Display Rules

## Prices and discounts

- Use the matched customer's recorded Orbit price band and the corresponding prices in the supplied price list. Band prices already include the discount; do not discount them again.
- An applicable contract replaces the band for the specified products. Oakfield's consumables receive 15% off list; other items use band T. Pasta Fresca's blue roll is £15.50 per case; other items use band B.
- Check stated contract periods against the trigger email's received date. Missing, conflicting, expired or unsupported terms require review rather than an assumed price.
- Show customer-requested extra discounts as unapproved requests. Exclude them from calculated prices and flag them for Karen.
- For quantity amendments, retain the existing order's recorded price less its separate discount once. Flag differences from reference pricing; do not silently reprice the order.

## Money, totals and VAT

- Use decimal arithmetic. Round calculated unit prices to two decimal places using ROUND_HALF_UP, then calculate line totals. Round VAT on the net order total.
- Use 20% VAT for proposed calculations and flag the export's conflicting 17.5% setting.
- Convert VAT-inclusive quoted prices to ex VAT before comparison. Flag unknown VAT/price bases and price differences, including one-penny differences.
- Distinguish quoted net, VAT and gross amounts. Do not treat a VAT-only amount as the order total.
- Interpret Orbit unit prices as pence. Treat discounts of `0.05` and `5.0` as 5%; flag a bare `1` as ambiguous.
- If any line cannot be calculated, show only an explicitly incomplete subtotal for known lines—not a complete order total.

## Quantities and proposed changes

- Show quantities with their catalogue units. Do not silently convert units, multiply pack sizes or accept conflicting pack sizes. Fractional count-unit quantities require confirmation.
- Distinguish additional quantities from replacement totals. An existing 10 plus 2 becomes 12 total; a revised total of 12 does not become 22.
- For amendments, show only requested changes, with recorded old values where available. Omit unchanged exact dates or quantities when confirmed equal.
- Block reductions below dispatched quantities. Additions or removals affecting dispatched orders/goods require operations review.
- Label inferred product matches for checking. Do not automatically substitute obsolete products or assume replacement compatibility.
- Preserve unsupported amendment requests for manual review without inventing Orbit fields or status meanings.

## Dates and delivery details

- Normalise supported dates while preserving their original wording. Keep deadlines and week-commencing windows distinct from exact delivery dates.
- Flag unsupported relative dates and requested dates/window starts before the review date. Do not turn them into delivery promises.
- Keep the account address separate from the order's delivery destination. The export does not contain the latter: show the old order destination as unknown and any requested replacement as unverified, requiring a live Orbit check.

## Warnings and readiness

- Unresolved customer matches block entry and show a prominent warning. Do not assign another customer's history.
- A resend must state **“Do not create another order.”** A matched existing order against a new-order request also blocks duplicate entry. No snapshot match does not prove an order is new.
- Recorded hold/on-stop and closed-account restrictions block entry. Customer claims cannot clear them. Proforma-only terms require Tom's payment confirmation for this request; an older paid order does not satisfy them.
- Keep **“Checks required”** when no implemented blocker is found; this still requires manual review. Use **“Do not proceed”** when a blocker or validation failure remains.
- When blocked, label the entry section **“Proposed changes — do not apply yet.”** Preserve earlier blocking alerts, including missing evidence and OCR checks.

## Evidence and qualifications

- Distinguish explicit requests, recorded values, inferred suggestions and unknowns. Inferred suggestions need an explanation and supporting evidence.
- Reject unsupported source references, products and amendment targets. Keep history separate from the current request and matched order: use the last three orders overall, plus independently retrieved explicit references.
- Show recorded status codes and dispatched quantities with snapshot timestamps. Do not invent undocumented code meanings or treat spreadsheet status/Who as verified authority.
- Flag OCR-derived details for manual verification. Retain source locations and available timestamps; generation time is not source freshness.

**All values remain proposals for human review. Current credit, pricing, stock and delivery feasibility must be checked before manual entry into Orbit.**
