# Meridian order preparation prototype

This prototype prepares customer orders and amendments for staff to review and enter manually in Orbit. It reads the supplied Meridian data pack and generates a timestamped HTML report with proposed entry fields, outstanding decisions and supporting evidence.

## How to run it

The saved v5 report is in [`output/live/v5_run/`](output/live/v5_run/). Open the `meridian_reports_*.html` file in a browser. You can view it without an API key.

To generate a fresh report, run the following commands from the repository root.
```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

In the same terminal, run the following commands. Paste your OpenAI API key at the prompt and press Enter. The key is hidden as you type and is passed to the prototype through the `OPENAI_API_KEY` environment variable.

```sh
printf 'OpenAI API key: '
read -rs OPENAI_API_KEY
printf '\n'
export OPENAI_API_KEY

python -m meridian --run --model gpt-6-luna --output-dir output/live/v6_run

unset OPENAI_API_KEY
```

`gpt-6-luna` was used for v5. If your API account uses a different compatible model, pass its ID with `--model`. The live run sends inbox content and relevant order context to OpenAI. It reads the bundled `data/` pack by default. To use another pack, pass `--input-root /path/to/pack`.

The command prints the generated HTML path and saves the classification, preparation and validation results alongside it. Open the HTML file in a browser. Use a fresh output directory for each run to keep the results easy to compare.


## What I built and why I chose this workflow

The tool brings customer requests, purchase orders, Orbit records, pricing and account notes into a consistent HTML report. For each request, it shows the proposed order entry or amendment, any outstanding decisions and the supporting evidence. Staff can review the prepared fields and enter them manually in Orbit.

The workflow uses one LLM call to identify relevant emails. The code then gathers the customer and order context for each selected email and makes a separate call to interpret that request. Python applies the business rules, calculates prices and totals, and generates the report. Processing each request separately makes it easier to trace a proposal back to its sources. Outputs are timestamped and retained, so staff can review earlier requests alongside later amendments.

The discovery conversations highlighted two problems: order preparation took time, and mistakes were costly. Karen and Priya spent about three hours each per day keying orders. Preparing those orders meant gathering information from emails, attachments, Orbit and reference documents. Resent POs and amendments could lead to duplicate deliveries. Unclear product descriptions made it harder to select the right items, and missed credit checks exposed Meridian to financial loss.

I chose this workflow to give Priya the information she needs “on one screen”, with the proposed fields ready to review and the evidence available when she needs it. Staff keep control of what gets keyed, as Karen wanted, and the prototype works alongside Orbit, as Mike required.

## What I deliberately left out

1. **Automatically resolving ambiguity.** I flagged unreadable parts of scans, uncertain product matches and conflicting information for staff to clarify. Guessing a quantity, product code or pack size could lead to an incorrect order.
2. **Automating every inbox task.** I focused on preparing new orders and amendments. Delivery enquiries, invoice queries and unrelated requests were outside the initial scope, so I could concentrate on the workflow Karen and Priya identified as a priority.
3. **Batching requests by customer.** I processed each selected email separately to reduce the risk of mixing details from multiple orders or amendments for the same customer. I deferred batching until I could establish a baseline for reliability, usage and API costs.

## What surprised me in the data and how I handled it

The supplied sources sometimes disagreed, so I used the discovery notes, account guidance and shared policy to decide which source to use for each field. Where customers had duplicate accounts or multiple delivery sites, I used the account notes to identify the current account and kept the requested delivery site separate. Documented contracts took precedence over the standard price band for the products they covered, and Orbit remained the authority for credit status. The Orbit export recorded a VAT setting of 17.5%, which conflicted with the documented practice. I used the prototype’s 20% VAT policy for calculations and retained the export value in the evidence.

The other surprise was how inconsistently values were represented. Dates appeared as Excel serial numbers and in several written formats, Orbit stored prices in pence, and discounts appeared as both fractions and percentages. I found these differences by inspecting the raw exports and comparing them with the price list and purchase orders. I added rules to normalise supported values before comparing dates or calculating prices, while preserving the original values for review. If a value could reasonably mean more than one thing, the report flagged it for clarification.

## Where AI helped and what I corrected

I used AI to help develop the validation rules and iterate on the report layout. In the prototype itself, the model identifies relevant emails and interprets each request using the supporting records. Reviewing its output exposed two problems I corrected:

1. **Giving the spreadsheet too much authority.** The agent treated ambiguous or unrelated entries in the open-items log as reasons to block new orders. I changed the rules so the log provides supporting context. Credit decisions depend on the account status in Orbit.
2. **Producing a checklist instead of preparing the order.** Early reports asked Priya to repeat lookups the tool could already complete from the supplied records. I changed the report to put the prepared account, product codes, quantities, units and prices in the main view. It now flags the specific decisions that still need staff input and keeps the evidence available for review.

## What I would have added with some extra time

1. Automatic ingestion of all data sources: I deferred folder-wide discovery, which matters as new files are added. I would scan the data folder, extract supported formats, preserve source references and retrieve relevant evidence for each email.

2. Additional context retrieval: The prototype can leave questions unresolved when the answer is outside the supplied context. I would add a targeted lookup for the missing information before asking the user—for example, searching older orders for a previously purchased product.

3. Formal evaluations: I compared live runs but did not build a repeatable evaluation suite for model quality. I would create labelled examples and measure classification accuracy, extraction correctness, evidence support and unnecessary questions across repeated runs.

4. API failure recovery: Timeouts can leave reports incomplete. I would add a controlled way to retry only failed emails, preserving successful results and recording each attempt.
