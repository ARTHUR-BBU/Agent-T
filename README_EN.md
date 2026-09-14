<!-- README maintenance: keep README.md and README_EN.md aligned on capabilities, limitations, setup, data handling, and roadmap status. -->

<div align="center">

# Agent-T

[简体中文](README.md) · **English**

### Before you sign, ask the questions that matter.

**An AI contract review assistant that helps you spot clauses worth a closer look, understand their practical impact, and prepare concrete revisions for discussion.**

Procurement agreements · Lease agreements · Non-disclosure agreements (NDAs)

[Try it](#try-agent-t) · [Active verification](#it-doesnt-stop-at-flagging-an-issue) · [Real-world example](#a-real-world-example-has-the-goods-actually-been-accepted) · [Product direction](#from-reviewing-one-contract-to-understanding-an-entire-project) · [Setup](docs/getting-started.md)

</div>

---

## Read the contract, but still not sure?

When is payment due? When does the deposit come back? Who is responsible when something goes wrong? The terms that matter most are often buried in small, concrete details.

Agent-T gives you a structured first pass before signing. Upload a contract, review the clauses that deserve attention, trace each finding back to the original text, and ask follow-up questions such as “How could this affect us?” or “How could we revise it?”

**Make the first conversation count.** Agent-T is designed for legal and audit teams while remaining readable for business owners, finance teams, and managers who need to understand the same evidence and decide what to do next.

## Turn one contract into a practical list of questions

| What you need to know | How Agent-T helps |
|---|---|
| **Where should I start?** | Checks common issues for the selected contract type and lists terms that need attention or could not be found. |
| **Why does this matter?** | Shows an explanation, the quoted text, and its clause location so you can assess the issue in context. |
| **What else deserves a closer look?** | With an AI provider configured, adds reference observations about completeness, consistency, and practical impact for human review. |
| **What still needs my confirmation?** | Continues checking questions with incomplete evidence, possible missing attachments, or a need for business judgment, then groups the items that require a human decision. |
| **How should I raise or negotiate it?** | Lets you ask follow-up questions about flagged clauses and prepare explanations, revision suggestions, and draft language. |
| **How do I share the review?** | Exports a Word report covering checklist results, reference scores, and candidate findings; the review can also be read on mobile. |

AI features require a configured model provider. The current Word export does not include the AI observations or follow-up answers shown in the web interface, so check the report scope before sharing it.

## It doesn't stop at flagging an issue

Many contract tools behave like an airport scanner: a keyword triggers an alarm. Agent-T is being built to go one step further and behave like a careful review assistant—**when it finds a concern, it returns to the contract, looks for evidence, and only then decides what needs your attention.**

> **Raise the question first, then find the evidence. Make a judgment when the evidence supports it; when it does not, say what is missing.**

The current MVP includes the first bounded active-verification loop:

**Concern → retrieve the relevant clause → check the quote and facts → ask you → recheck the item**

- Verifies that a quoted passage exists in the contract and anchors it to a clause instead of supporting a conclusion with text that cannot be found.
- Keeps evidence-complete factual checks in the system while escalating broken evidence, inconsistent amounts, possible missing attachments, and judgment calls for human confirmation.
- Lets you add context, confirm or dispute an item, and then ask the system to check it again.
- Limits verification rounds and evidence retrieval. Active verification raises questions; it does not silently rewrite the original rule-based result.

The point is not to add one more “risk” badge. It is to turn a vague warning into three questions you can act on: **Where is the evidence? What could it mean in practice? What should happen next?**

### A real-world example: has the equipment actually been accepted?

A procurement agreement says:

> The buyer shall pay 95% of the contract price after delivery.

Read in isolation, this may only trigger a warning that the payment percentage is high. The real issue may sit across several parts of the contract:

- the acceptance clause says that the goods will be deemed accepted unless the buyer objects within three days of arrival;
- the technical appendix says that installation and testing take seven days;
- the payment clause uses “acceptance” as the payment trigger.

Active verification should keep asking: **Does delivery equal acceptance? When does the three-day clock start? How can the equipment be deemed accepted before testing is complete?**

When the evidence lines up, the user needs more than “please note the payment risk.” A useful result would be:

> The acceptance window is shorter than the testing period. The buyer may be deemed to have accepted the equipment, and become liable for 95% of the price, before performance has been verified. Consider starting the acceptance period after installation and testing, allowing seven business days for review, and requiring written acceptance before payment is triggered.

The result should also point back to the payment clause, the deemed-acceptance clause, and the technical appendix. Business, finance, and legal teams can then discuss the same facts.

<details>
<summary><strong>Two more examples: security deposits and pay-when-paid clauses</strong></summary>

**Lease security deposit**

A lease says that the landlord will return the deposit within 60 days after confirming that there is no damage or unpaid balance. Active verification should ask who decides whether damage exists, whether ordinary wear counts as damage, what happens if the landlord never confirms the handover, and whether there is a signed handover checklist or an objection deadline.

If those terms are missing, the issue is more than a slow refund: the landlord may control when the refund clock starts. A more actionable proposal could require both parties to sign a handover record, give the landlord five business days to raise written objections, and require the undisputed balance to be returned within ten business days.

**Pay-when-paid clause**

A subcontract says, “Party A will pay Party B after Party A receives payment from the end customer.” Active verification first needs the parties' enterprise sizes, the contract type, the signing date, and the applicable law. It should then ask what happens if the end customer never pays, whether Party A caused the non-payment, whether there is a maximum waiting period after Party B has performed, and what evidence Party A must provide.

This is not merely a drafting problem. In mainland China, a large enterprise making third-party payment a condition for paying an SME in construction, goods procurement, or services may raise a direct question about the clause's validity. The [Supreme People's Court Reply, Fa Shi [2024] No. 11](https://www.court.gov.cn/fabu/xiangqing/441281.html) addresses that situation. Outside its scope, the parties' status, transaction, and specific wording still need separate analysis. The Agent should establish those facts before suggesting a revision or course of action.

</details>

## When Agent-T is worth a first look

### Before purchasing: make payment and acceptance concrete

A supplier sends over a contract. You need to understand the payment schedule, acceptance process, warranty, and remedies before the commercial conversation starts.

### Before leasing: understand the cost of getting out

Before signing for an office or operating site, check the deposit, early termination, maintenance, and reinstatement obligations. Go beyond “What is the rent?” and ask “How do we leave, who fixes what, and what will it cost?”

### Before sharing information: define what must remain confidential

For an NDA, review the scope of confidential information, agreement term, survival period, liability, and intellectual-property terms from the correct party's perspective.

These examples illustrate intended use cases. They do not mean the system will detect every possible issue.

## Complete a review in five steps

**Upload → review findings → resolve items to verify → ask deeper questions → take suggestions into the conversation**

1. **Choose the file and your side.** Select a contract type and the party you represent, then upload one contract. If AI pre-checking suggests a different type, Agent-T asks you to confirm.
2. **Review each item.** Read the checklist result, explanation, source quote, and optional AI observations.
3. **Resolve what still needs confirmation.** Add context, confirm an item, or dispute it, then recheck that item without changing the original checklist status.
4. **Ask a specific question.** For example: “How could this affect payment?” or “Draft a balanced revision we can discuss with the other party.”
5. **Verify before using the output.** Compare every suggestion with the contract, copy the language you want to discuss, or export a report for colleagues.

## What is supported today?

| Contract type | Current review perspective | Main areas checked |
|---|---|---|
| Procurement agreement | Buyer; defaults to a buyer-oriented explanation when no side is declared | Parties, subject matter, payment, delivery dates, breach, warranty, and related terms |
| Lease agreement | Tenant; defaults to a tenant-oriented explanation when no side is declared | Term, rent, deposit, handover, maintenance, alterations, and early termination |
| Non-disclosure agreement | Disclosing party or receiving party | Confidentiality scope, agreement term, survival, liability, and intellectual property |

The selected stance is recorded and used for explanations, while the current rule-based checklist itself does not change with stance. Seller-side procurement and landlord-side lease review are not yet supported.

The current interface, rule packs, and samples are Chinese-first. Agent-T accepts text files, Word (`.docx`), and text-based PDFs when the PDF parsing dependency is installed. Legacy `.doc` files and scanned documents depend on the local parsing environment; converting them to `.docx` or a searchable PDF is recommended. Each file is limited to **10 MB**, and each review currently handles one contract.

## From reviewing one contract to understanding an entire project

> **A contract tells us what should happen. Project records tell us what actually happened. Active verification compares the two.**

Agent-T currently focuses on one contract at a time. The longer-term direction is a project evidence workspace that extends review from the moment of signing into performance, acceptance, payment, change control, and claims.

Think of the future system this way:

- **The project document store** is the archive: the main contract, amendments, specifications, meeting minutes, delivery records, acceptance reports, remediation logs, invoices, and payment requests.
- **RAG retrieval** is the archivist who finds potentially relevant passages for a question.
- **The active-verification Agent** is the reviewer who decides what to investigate next and whether the evidence is complete, current, and consistent.
- **The evidence chain** is the resulting case file, allowing every conclusion to be traced to a document, version, page, and exact passage.

Suppose finance asks, “Can we pay this 95% equipment invoice now?” The Agent should do more than repeat the payment clause. It should check:

1. whether the latest amendment changed the payment percentage;
2. whether both parties signed the acceptance report;
3. whether all remediation items have been closed;
4. whether the invoice and payment request match the latest agreed amount;
5. which missing documents prevent a reliable decision.

If the main contract says 95%, a later amendment says 80%, the acceptance report lacks the buyer's signature, and two remediation items remain open, a useful result would be:

> The available records do not establish that the contractual acceptance condition has been satisfied. The latest amendment also changes this payment to 80%. Do not process a 95% payment until the buyer-signed acceptance record and evidence that the remaining remediation items are closed have been provided.

The evidence chain should identify the source document, version, relevant clause, exact passage, and why it supports the conclusion.

| Product stage | What Agent-T is intended to solve |
|---|---|
| **Now: active verification within one contract** | Locate supporting text, route unresolved issues for human confirmation, and recheck an item after the user adds context. |
| **Next: a revision-verification loop** | Offer revision options with different tradeoffs and check whether one change affects related payment, liability, invoicing, or timing clauses. |
| **Long term: a project evidence workspace** | Connect contractual obligations with performance records across documents and detect version changes, conflicting evidence, missing materials, and deadlines. |

Multi-document project workspaces and RAG are **not implemented today** and are outside the current MVP. The goal is not simply to attach a vector database. Retrieval must respect document permissions, version history, and evidentiary weight, and the Agent must say what is missing when the evidence is insufficient.

## Evidence first, judgment preserved

Agent-T keeps **rule-based checklist results** separate from **AI reference opinions**. AI-generated candidate issues require human confirmation, and reference scores do not overwrite item-level rule results.

Agent-T is useful for preparing questions, understanding provisions, and drafting language for discussion. It is an evolving MVP and may produce false positives, miss issues, or cite text inaccurately; long-document AI review is also limited by segmentation budgets. Check every citation and proposed revision against the source contract, and have important agreements reviewed by a qualified professional.

## Try Agent-T

**Start with one of the included samples and walk through the complete flow.**

- [Procurement sample](fixtures/procurement_sample.txt): explore payment and acceptance checks.
- [Lease sample](fixtures/lease_sample.txt): explore deposit and termination terms.
- [NDA sample](fixtures/nda_public_template.txt): explore a clause-by-clause confidentiality review.

Agent-T is currently self-hosted. Follow the [setup and configuration guide](docs/getting-started.md) to start it locally, then upload a sample in your browser. The detailed guide is currently in Chinese.

**You can use the rule-based checklist and export a report without a model API key.** Configure DeepSeek, Zhipu AI, or xAI to enable AI pre-checking, reference analysis, and clause follow-up. Model usage is billed by the selected provider.

### Where does contract data go?

Contracts are parsed and stored on the server where you deploy Agent-T. With no model key configured, Agent-T does not call an LLM. When a provider is configured, relevant contract text and questions are sent to that provider, or to your custom gateway if one is configured. Review records become inaccessible after 24 hours by default; retention and cleanup behavior are configurable and documented in the setup guide.

Use a sample or redacted contract for your first run. Before exposing Agent-T to the internet or uploading real contracts, configure authentication and HTTPS.

## For developers and contributors

Agent-T uses **FastAPI + LangGraph + SQLite** with a lightweight web interface. Rule configuration, AI reference analysis, and presentation are separated so each contract scenario can be improved independently.

| What you need | Start here |
|---|---|
| Local setup, model configuration, tests, and API | [Setup and configuration](docs/getting-started.md) — Chinese |
| Server deployment | [Deployment guide](docs/deploy-server.md) — Chinese |
| Rule, budget, and rate-limit configuration | [Administrator guide](docs/admin-config.md) — Chinese |
| Product direction and roadmap | [Development roadmap](docs/roadmap-llm-ui.md) — Chinese |
| Iteration history | [Plain-language changelog](docs/plain-changelog.md) — Chinese |
| Interface design rules | [Design notes](docs/design-tokens.md) — Chinese |

Found a false positive, a missed issue, or an explanation that is hard to understand? [Open an issue](https://github.com/ARTHUR-BBU/Agent-T/issues) with a **redacted excerpt, contract type, expected result, and actual result**. A concrete example is the best starting point for the next improvement.

---

<div align="center">

**Turn contract uncertainty into clear questions for the negotiating table.**

**From reviewing one contract to understanding an entire project.**

If this direction matters to you, Star the repository to follow the work—or share one real scenario that can help Agent-T improve.

</div>
