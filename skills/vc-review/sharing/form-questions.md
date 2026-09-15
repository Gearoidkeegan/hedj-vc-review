# Feedback form: setup and questions

The anonymous result is collected with an ordinary web form that supports pre-filled
links (Google Forms has "Get pre-filled link"). The plugin never sends anything: it
builds a link with some answers filled in, and the reviewer submits the form
themselves. Until the form is set up, the sharing step only prints the result.

## 1. Create the form

Title: **VC Review: anonymous result**

Description (edit the last sentence to say who runs the form):

> VC Review is a free Claude Code plugin. This form collects anonymous results so we can
> check how well its scores match reality. The first questions arrive filled in from your
> review; correct any you know better. Please don't include names, company details or
> anything else that identifies a deal, and don't share if a confidentiality agreement
> rules it out. Results are used only in aggregate, to improve the scoring. Run by
> [who runs the form, and a contact address].

Settings, so responses stay anonymous:

- Don't collect email addresses, and don't require respondents to sign in.
- Don't limit people to one response (that setting requires a sign-in).
- In Microsoft Forms, choose "Anyone can respond" and turn off recording names.

## 2. Questions

### Part 1: filled in from the review

Make every Part 1 question a **Short answer** (text) question, titled exactly as below,
so the pre-filled link can carry the answer. The key is what you type in step 3.

| # | Question title | Key | Answers the plugin sends |
|---|---|---|---|
| 1 | Plugin version | `version` | e.g. 2.1.0 |
| 2 | Month | `month` | e.g. 2026-09 |
| 3 | Stage | `stage` | pre-seed, seed, series-a, later, not stated |
| 4 | Depth | `depth` | full, quick |
| 5 | Model mode | `models` | judicious, max, not stated |
| 6 | Investability score (nearest 5) | `score` | 0 to 100, in steps of 5 |
| 7 | Documents-corrected score (nearest 5) | `corrected` | 0 to 100 in steps of 5, or not computed |
| 8 | Recommendation | `recommendation` | pass, further due diligence, invest |
| 9 | AI-usage score | `ai_usage` | 0 to 10, or not run |
| 10 | Hidden text found | `hidden_text` | yes, no, not checked |
| 11 | Pilot completed | `pilot` | yes, no, not stated |
| 12 | Paying customers | `paying` | yes, no, not stated |
| 13 | ARR band | `arr` | pre-revenue, under EUR 100k, EUR 100k-500k, EUR 500k-1m, EUR 1m-5m, over EUR 5m, not stated |
| 14 | Funding raised to date | `funding` | none, under EUR 500k, EUR 500k-2m, EUR 2m-10m, over EUR 10m, not stated |
| 15 | Round size | `round` | under EUR 500k, EUR 500k-1m, EUR 1m-3m, EUR 3m-10m, over EUR 10m, not stated |
| 16 | Sector | `sector` | b2b software, fintech, health, climate and energy, deep tech and hardware, consumer, marketplace, other |

Add help text to questions 11 to 16: "Filled in from the materials. Change it if you know
better." Amounts in other currencies go in the nearest equivalent band.

### Part 2: answered by the reviewer

| # | Question | Type | Options |
|---|---|---|---|
| 17 | Your role | Multiple choice | Investor (angel, VC, family office); Founder reviewing my own company; Adviser, accelerator or incubator; Other |
| 18 | What has happened with this company so far? | Multiple choice | Passed; Still in diligence; Invested; Not applicable |
| 19 | Do you agree with the recommendation? | Multiple choice | Agree; Too harsh; Too generous; Not sure |
| 20 | How helpful was the review? | Linear scale | 1 (not helpful) to 5 (very helpful) |
| 21 | Anything we should improve? | Paragraph, optional | Help text: "No names or details that identify the company, please." |

## 3. Connect the form to the plugin

1. Open the form's pre-filled link option.
2. Answer each Part 1 question with its key from the table: type `version` into
   Plugin version, `month` into Month, and so on through `sector`. Leave Part 2 blank.
3. Copy the pre-filled link, then from the repository root run:

   ```
   python skills/vc-review/scripts/share_result.py --configure "<the link>"
   ```

4. It writes `skills/vc-review/sharing/share-config.json`. Commit that file and publish
   a new version; from then on the sharing step gives reviewers a working link.

If the script says the link doesn't carry an answer for some keys, that question isn't a
Short answer, or its key wasn't typed exactly.
