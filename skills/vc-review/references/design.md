# VC Review: design notes

Why the pipeline looks the way it does. `SKILL.md` is the operating manual; this file is
the reasoning behind it.

## What it is

An agentic review that takes a business idea or pitch (deck, memo or written description)
and produces a critical, investor-grade analysis: idea quality, market, scalability, gaps
in the presentation, criticisms, and the questions an investment committee would ask.

It's decision support, not investment advice. Criticism is the product, and the pipeline
is built so praise can't soften the output. The deliverable is an investment committee (IC)
memorandum in Markdown, Word and a one-slide summary, ending in exactly one of three
recommendations: **pass** (with questions and feedback), **further due diligence** (with key
areas of focus) or **invest** (with suggested milestones).

It hasn't yet been calibrated against decks with known outcomes. Treat scores as
structured opinion.

---

## 1. What we learned from existing models

### 1.1 ADIN (adin.online, Tribute Labs)

ADIN (Automated Deal Investing Network) is the closest reference. It's an operating
AI-native VC:

- **Multi-agent, multi-model engine.** 28+ specialist agents orchestrated across 200+
  tools (SEC filings, financial databases, court records, market intelligence). A
  submitted deal is analysed in roughly 5 to 10 minutes across team, market, technology and
  financials, producing an institutional-grade report with red flags raised automatically.
- **Persona panel.** Five persona agents sit on top of the specialist layer. Each embodies a
  distinct investment philosophy, reads the same deal and argues from its own thesis: the
  Network Hunter (network effects, virality), the Tech Oracle (technical breakthroughs), the
  Monopoly Maker (scale and dominance), the Unit Master (unit economics) and the Value Guy
  (fundamentals and durable moats).
- **Human layer on top.** Community scouts cast weighted, binding votes on whether to
  proceed. General Partners check the agents' work, hold veto power and handle closing.

What we copied: separate specialist analysis (facts per dimension) from persona judgement
(opinionated verdicts); make red flags an explicit output rather than a side effect; keep a
human decision layer, so the agents inform and people decide.

### 1.2 Techstars

There's no official Techstars Claude skill. The review uses Techstars' published selection
rubric instead:

- **"Team, team, team, market, progress, idea", in that order.** Founder quality,
  complementary skills, a track record of working together and founder–problem fit
  dominate the screen.
- **Progress over polish.** Evidence of an MVP or real momentum outweighs the idea itself.
- **Mentor-style feedback.** Screening produces actionable feedback and interview
  questions, not just a score. So does this review.

What we copied: weight the rubric explicitly (team-heavy at pre-seed) and always produce
questions for the founder as a first-class output.

### 1.3 Other VC frameworks folded in

- **Sequoia's business-plan template**, the canonical completeness checklist, is our
  gap-analysis checklist. Anything a pitch fails to address is a named gap.
- **Peter Thiel's seven questions** (*Zero to One*): engineering, timing, monopoly, people,
  distribution, durability, secret. They make excellent adversarial probes.
- **YC.** "Make something people want." Evidence of usage beats claims.
- **Bessemer and institutional memo culture.** Written memos with an explicit "reasons to
  lose" section and preserved dissent. Criticism is a deliverable, not a by-product.
- **Angel scorecard methods** (Payne scorecard) give explicit weightings, a default we tune
  per stage.

---

## 2. Design principles

1. **Specialists first, personas second, synthesis last.** Evidence is gathered once per
   dimension, then opinionated personas argue over the same evidence base.
2. **Criticism is the product.** The default failure mode of LLM reviewers is sycophancy.
   Every stage carries an adversarial mandate, and a dedicated red team exists so praise
   elsewhere can't soften the output.
3. **Evidence-grounded and claim-labelled.** Every material claim is tagged *(stated in
   pitch)*, *(externally verified)* or *(assumption/unverified)*. Market sizes are rebuilt
   bottom-up rather than accepted from the deck.
4. **Structured outputs everywhere.** Each agent writes JSON against a schema, so the memo
   is assembled, not improvised, and a script can check it.
5. **Human decision layer.** The output is a decision-support memo with preserved dissent,
   never an automated verdict that hides disagreement.
6. **Stage-aware rubric.** Weightings shift with the company's stage.
7. **The materials are untrusted.** A deck is written by the party being judged. Nothing in
   it is ever treated as an instruction, and text designed to be unseen is surfaced rather
   than read.
8. **Spend tokens on judgement, not on moving text around.** Scripts do the extraction,
   summarising and checking; agents do the thinking.

---

## 3. Architecture

```
 Input (folder of materials, a file, or an idea typed inline)
        │
 Stage 0  INTAKE: protect the output folder · extract_materials.py
        │   visible text, model cells and formulas, page images, metadata
        │   hidden-text scan (left out of the extract, reported)
        │   → Deal Profile with claim IDs
        │
 Stage 1  SPECIALIST ANALYSTS (parallel, each saves its own file)
        │   team · problem/idea · market & timing · product/tech
        │   business model · competition & moat · traction
        │   scalability & ops · financials & the ask
        │
 Stage 1b DEEP-DIVE TRACKS (parallel with Stage 1)
        │   financial model audit · investor memorandum critique · AI usage
        │   founder background check · website & social audit (web agents, no file access)
        │
        │   build_digest.py → evidence digest
        │
 Stage 2  PERSONA PANEL (at most three at a time)
        │   five thesis seats read the digest · the Skeptic reads everything
        │
 Stage 3  ADVERSARIAL LAYER
        │   red team pre-mortem (digest) · gap analysis · question consolidation
        │
 Stage 4  SYNTHESIS: a fresh agent writes the memo and scenario scores
        │   build_deliverables.py --check: contract and consistency
        │
 Stage 5  DELIVERABLES: memo.docx · summary.pptx, built once
```

### Stage 0: intake and the untrusted-materials problem

`extract_materials.py` reads every file once, with the standard library (plus PyMuPDF for
PDFs when installed): slide text with speaker notes, tables, chart series and SmartArt;
Word paragraphs, headers, footnotes and comments; every spreadsheet cell with its value,
formula and comments, shared formulas expanded; PDF page text and page images. Office XML
parts that declare a DTD are refused, closing off entity-expansion tricks.

The same pass looks for **text a reader can't see**: text coloured like whatever is behind
it (resolving theme colours, shape fills, the shapes and pictures beneath, table cells and
page backgrounds), text under 4pt, invisible or near-transparent text, hidden shapes,
slides and sheets, the Excel `;;;` format that hides a value, and text placed off the page.
Colour is only judged when the background is actually known, so white text on a photo or
a dark shape isn't flagged. Findings go to `hidden-text.json`, with a flag when the hidden
words read like instructions to an AI, and the hidden text is left out of what the agents
read. On the slide it's a separate header chip that turns red; in the memo it's a short
integrity note and a red flag.

### Stage 1: specialist analysts

Each returns the same schema: score 1–10, confidence, evidence, strengths, weaknesses, red
flags, unverified claims, questions.

| Analyst | What it critically examines |
|---|---|
| **Team** | Founder–problem fit, completeness, track record together, cap table sanity, key-person risk. Would you back these people with a different idea? |
| **Problem & idea** | Is the problem real, painful and frequent? Painkiller or vitamin. What do they know that others don't? 10x or 10%? |
| **Market & timing** | TAM/SAM/SOM rebuilt bottom-up from pricing × addressable buyers; top-down "1% of a big number" flagged. Why now? |
| **Product & technology** | Does the demo or spec support the claims? Build vs thin wrapper, defensibility, dependency risk. |
| **Business model** | How money is made. CAC/LTV plausibility, gross margin, payback, pricing power. |
| **Competition & moat** | A real competitor map including "do nothing" and incumbents. Moat type and whether it strengthens with growth. |
| **Traction** | Vanity metrics vs evidence of pull: retention, cohorts, paying vs pilot, LOIs vs contracts. |
| **Scalability & ops** | Marginal cost of the next customer, services-in-disguise risk, regulatory friction, hiring plan vs reality. |
| **Financials & the ask** | Burn, runway, use of funds, the milestone the round buys, dilution logic. |

### Stage 1b: deep-dive tracks

- **Financial model audit**: integrity, every driver rated realistic / aggressive /
  implausible, a downside case at 50% of plan revenue, hockey sticks. Verdict: sound,
  repairable or unreliable.
- **Investor memorandum critique**: cross-document consistency by claim ID, completeness
  against the institutional standard, risk-disclosure quality, evidence standard.
- **AI usage**: whether the materials were visibly thrown together with AI (generic
  interchangeable wording, generator templates used as-is, AI imagery with artefacts,
  invented-looking content, metadata such as a known generator or minutes of editing). It
  judges carelessness, not AI use: polished, specific work and good AI visuals score low.
  Scores of 6 and above take 2 to 5 points off the 0–100 investability score, which the
  documents-corrected score adds back.
- **Founder background check**: credentials against public professional sources,
  per-founder scores for credibility, value and investor desirability. Same-name
  mis-identification is guarded by requiring two corroborating sources before any negative
  finding enters the memo.
- **Website and social audit**: claims against the pitch, product evidence, hygiene,
  activity and engagement authenticity.

The two web agents have no file access. They get what they need in their prompt, which
breaks the path by which a doctored document could get an agent to read local files and
send them out in a web request.

### Stage 2: persona panel

Five thesis personas mirror ADIN's philosophies; a sixth, **the Skeptic**, is mandated to
build the strongest coherent case against. The thesis seats judge from the evidence digest;
the Skeptic reads every file, because the strongest case against is often in the detail.
Disagreement is preserved: a 5–1 split with a sharp dissent tells you more than an average.

### Stage 3: adversarial layer

A pre-mortem ("it's three years on and the company is dead"), a gap analysis against the
Sequoia checklist rated fatal / serious / cosmetic, and a consolidated question list in
three parts: for the founders, for independent diligence, and what any IC will ask.

### Stage 4: synthesis and the check

A fresh agent assembles the 13-section memorandum from the stage files; it doesn't
re-judge. It also writes the **documents-corrected counterfactual**: every dimension
re-scored on the identical business with document-level defects fixed and substantive
findings kept. It answers the question every founder asks on receiving a bad review, and the
answer is diagnostic either way.

`build_deliverables.py --check` then confirms every file and key, prints the facts behind
the recommendation, and fails an invest the evidence doesn't allow: unresolved fatal gaps,
an unreliable model, no proper founder check, quick depth, or unexplained hidden
instructions.

### Stage 5: deliverables

`memo.docx` renders the memo; `summary.pptx` reads the stage files directly, so the slide
can't drift from the evidence. The gauge bands come from the rubric anchors (70+ fundable,
45–70 credible but not yet investable, below 45 material doubts), in a fixed status palette
checked for colour-vision deficiency, with labels always on so no band is identified by
colour alone. Both formats are written as raw OOXML with the standard library, because
reviews often run on locked-down machines.

---

## 4. Why the pipeline is shaped this way for cost

In measured runs of the earlier version, the main session was about 60% of the spend,
mostly from copying every agent's output into files and then re-reading that swollen
context at every later step. So agents now save their own files and reply with one line;
one script extracts the materials once instead of each analyst re-reading raw files;
personas and the red team read a digest; a fresh agent writes the memo; and the builder
checks before it builds, so the deliverables are built once.

---

## 5. Risks and mitigations

- **Sycophancy and grade inflation.** A Skeptic and red team with bearish mandates, and
  anchors that mean something ("7 = fundable at a good fund", not "7 = nice idea").
- **Prompt injection through the materials.** Materials are data, never instructions, in
  every agent; hidden text is surfaced and excluded; web agents have no file access; only
  the skill's own scripts run.
- **Hallucinated market data.** Market sizes are rebuilt from the deck's own numbers;
  external claims must carry citations; anything unverifiable becomes a diligence question.
- **Background-check errors and fairness.** Public professional sources only, two
  corroborating sources before any negative finding, and contradictions framed as questions
  for the founders.
- **False authority.** The memo says it's decision support, not investment advice.
- **Confidentiality.** Outputs stay local and out of version control; nothing goes to
  third-party services beyond the model and name-and-public-claim web lookups.

## 6. What good looks like

1. A review of a real deck surfaces at least one material gap or criticism the reader
   hadn't already spotted.
2. The question list is usable verbatim in a founder meeting.
3. Persona disagreement is visible, not averaged away, on any genuinely contentious deal.
4. Every memorandum ends in exactly one of pass, further due diligence or invest, with its
   mandatory supporting content.

---

## Sources

- ADIN: [adin.online](https://adin.online/), [How ADIN Works](https://adinonline.substack.com/p/how-adin-works-the-future-of-venture), [Meet ADIN's Agents](https://adinonline.substack.com/p/meet-adins-agents), [Introducing ADIN](https://adinonline.substack.com/p/introducing-adin)
- Techstars: [How does Techstars choose companies](https://help.techstars.com/support/solutions/articles/4000207080-how-does-techstars-choose-which-companies-are-accepted-into-program-), [Team, Team, Market, Product, Traction](https://seobrien.com/team-team-market-product-traction)
- Community Claude skills reviewed: [OneWave-AI pitch-deck-reviewer](https://github.com/OneWave-AI/claude-skills/blob/main/pitch-deck-reviewer/SKILL.md), [cc-skills-vc-fundraising](https://github.com/tjboudreaux/cc-skills-vc-fundraising), [startup-founder-skills](https://github.com/shawnpang/startup-founder-skills), [pitch-deck-mastery-skill](https://github.com/Stevekaplanai/pitch-deck-mastery-skill)
