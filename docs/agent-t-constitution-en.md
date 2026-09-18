# Agent-T Constitution

> **Status: Normative engineering governance document**  
> **Scope: Agent-T rules, AI, evidence, workflow, state, tests, code review, and release process**  
> **Core principle: Rules decide. AI challenges. Evidence constrains. Procedure checks power. Humans retain final responsibility.**
> **Companion implementation standard: [LLM Position, Authority, and Phased Development Standard](llm-position-authority-phased-development.md)** *(Chinese)*

---

## Preamble

Agent-T is a contract-review application, but the engineering problem is broader:

> **What gives the system authority to call something a fact, to call that fact a risk, to challenge a formal result, or to change the rules themselves?**

Without explicit answers, a multi-agent system easily degenerates into several models repeatedly reinterpreting the source, recreating evidence, and overwriting one another.

This document is therefore not marketing copy and not merely an architecture note. It is the project's **normative boundary of authority**.

Any feature, refactor, Rule Pack change, prompt change, evidence change, new agent capability, or workflow transition should answer:

1. Which institutional role does it belong to?
2. What authority does it gain?
3. What authority does it explicitly not have?
4. Who can check, reject, or correct it?
5. If it changes a formal result, are the evidence and procedure complete?

---

# I. Constitutional principles

## 1. Facts, claims, and decisions are separate

Agent-T must distinguish:

- **Fact / Evidence** — what the source actually says, where it appears, and in which version.
- **Claim / Argument** — what that fact may mean and why it should be challenged.
- **Decision / Status** — the formal result produced under an applicable deterministic rule.

They must not be collapsed into one untraceable generated string.

## 2. Deterministic authority outranks generative authority

Formal Rule Results should be produced by explicit, testable, reproducible logic wherever practical.

An LLM may discover concerns, raise objections, add context, draft explanations, or identify gaps in the Rule Pack.

An LLM must not, from one generation alone:
- overwrite a Rule Result;
- amend the Rule Pack;
- change applicability;
- promote unverified text into a formal fact;
- represent its own opinion as a confirmed system result.

## 3. Evidence first

A material claim should answer:
- Where did the evidence come from?
- What is the exact passage?
- What clause / span / document / version contains it?
- Is it relevant to this issue?
- Was it actually in the system or model's visible/retrieved scope?
- Is there counter-evidence?

**Existence is the minimum bar; relevance is the admissibility bar.**

## 4. Due process

A result is not trustworthy merely because it looks right.

Required stages, completion semantics, failure states, review, and CI must not be bypassed to obtain a convenient output.

## 5. Human responsibility remains

Agent-T may automatically produce formal rule-check results. Human users retain final responsibility for contract signing, legal commitments, commercial tradeoffs, and real-world actions.

When uncertain, the system should expose what is missing rather than simulate certainty.

---

# II. Separation of powers

## 6. Legislature — Rule Governance

**Counterpart:** maintainers, domain/legal experts, Rule Pack governance, PR review, CI, version control.

### May
- create, amend, and retire rules;
- change rule classes and applicability;
- change governance-level prompts and gates;
- change formal procedure through reviewed, tested code;
- promote repeated AI discoveries into rules or tests.

### Must not
- allow a runtime model to amend the Rule Pack;
- silently change general law for one convenient case;
- alter formal decision logic in production without traceable review and tests.

AI may **propose legislation**. It may not **enact legislation**.

---

## 7. Law — Rule Packs and hard constraints

Rule Packs are the system's substantive law.

They define:
- which facts create attention states;
- which absence creates an omission;
- hardline / existence / heuristic classes;
- what may be admitted to the objection layer;
- forbidden outputs and hard constraints.

Rules must be versioned, testable, and traceable.

---

## 8. Procedural law — Graph / Pipeline / State Machine

LangGraph, pipeline stages, state transitions, and event semantics define procedural law.

They determine:
- stage order;
- required completion;
- soft-degradation boundaries;
- transition requirements;
- when `fully_complete` is legally true;
- how unavailable / partial / failed must be represented.

A completion event is a governance semantic, not UI copy.

---

# III. Investigative power — Police / investigators

## 9. Investigation layer

Includes:
- Parser
- Clause Index
- Quote Locator
- Retrieval
- Verify
- document parsing
- future RAG and multi-document evidence retrieval
- structured user fact collection

### May
- locate source text;
- find related clauses;
- extract dates, amounts, parties, and obligations;
- construct candidate evidence;
- report missing material;
- disclose coverage limits.

### Must not
- issue the final risk judgment merely because text was retrieved;
- substitute similar text for issue-specific evidence;
- hide partial-document coverage;
- treat model-generated quotations as source truth;
- decide the governing legal conclusion by itself.

Investigation answers **“What happened, and where is the evidence?”**, not **“What is the final judgment?”**

---

# IV. Admissibility — Procuratorate / evidence gate

## 10. Evidence and candidate admission

Includes:
- Evidence Validation
- Quality Gate
- Candidate Admission
- Applicability Check
- Quote Verification
- Scope Validation
- structured-output validation

Before a candidate is admitted, the gate should test at least:

1. authenticity;
2. locatability;
3. relevance;
4. visibility within retrieved/model-seen scope;
5. completeness;
6. applicability;
7. counter-evidence.

### Must not
- accept a quote merely because it is true somewhere in the contract;
- launder unrelated truth into support for another issue;
- replace the Rule Engine;
- weaken gates merely to improve model acceptance without recording the change.

**True evidence is not necessarily relevant evidence.**

---

# V. Adjudication — Court and judge

## 11. Court

The Court is the formal decision process, not one function.

It receives admissible facts, the current Rule Pack, and explicit context, and produces traceable formal results.

## 12. Judge — Rule Engine / Checklist Engine

Core function:

> **Fact + Applicable Rule → Formal Decision**

### May
- issue formal statuses under the Rule Pack;
- select the applicable rule and primary evidence;
- reject a formal determination when conditions are not met.

### Must not
- change objective facts because of stance;
- overwrite rules because an LLM sounds more persuasive;
- change decision behavior without versioned governance;
- equate “not retrieved” with “does not exist” unless the rule explicitly defines that inference.

Same input + same rule version + same deterministic configuration should, as far as practical, yield the same formal decision.

---

# VI. Advocacy and objection — AI counsel

## 13. AI Objection Layer

The objection model is not a second judge. It is bounded counsel.

### May
- raise false-positive objections for admitted heuristic findings;
- raise omission objections for admitted existence cases;
- find exceptions, counter-evidence, equivalent wording, or missing context;
- identify gaps in current rules;
- generate explanations, negotiation questions, and revision proposals;
- recommend that a repeated pattern be promoted into a Rule Pack.

### Must not
- overwrite a Rule Result;
- choose its own authoritative `rule_id`;
- expand its own candidate set;
- use out-of-scope evidence to validate itself;
- treat unverified quotes as evidence;
- present advocacy as a court judgment;
- consume the global objection quota through repeated versions of the same issue.

## 14. One issue, one dispute identity

As a default, one `(item_id, direction)` represents one dispute.

A dispute may carry multiple evidence items, but several quotations for the same dispute should not consume several global objection slots.

---

# VII. Parties and stance

## 15. Stance boundaries

Buyer / Seller, Tenant / Landlord, Discloser / Receiver and similar stance affect:
- whose interests are being evaluated;
- which consequences deserve emphasis;
- how revisions are framed;
- negotiation strategy.

Stance must not change:
- source text;
- numbers;
- dates;
- clause locations;
- quotes;
- objective match facts.

**Stance changes evaluation, not facts.**

---

# VIII. Evidence law

## 16. Single Evidence Fact

A deterministic risk match should, whenever practical, create one stable Evidence Record.

Status, quote, hits, primary clause, AI explanation, objection, follow-up, and report should reuse that record rather than independently rediscovering the fact.

> **One risk match, one evidence fact; downstream layers reference it.**

## 17. Evidence provenance

A mature Evidence object should contain:
- source/document id;
- version;
- clause id;
- start/end span;
- exact quote;
- normalized fact;
- producer;
- validation state.

In multi-document workflows it should additionally track:
- document precedence;
- effective time;
- superseding amendments;
- permissions;
- conflicts.

## 18. Coverage must be explicit

If the system examined only:
- the first N characters;
- 12 candidates;
- some attachments;
- selected clauses;

then coverage must be recorded.

“Not found in the examined scope” must not silently become “does not exist in the contract.”

Coverage is evidence metadata, not debugging trivia.

---

# IX. Case file, clerk, and audit state

## 19. State Store

SQLite / Review State / Evidence Chain / Audit Log are the case-file and clerk system.

They may preserve:
- state;
- evidence;
- rule version;
- human confirmation;
- AI objections;
- failures and coverage;
- traceability.

They must not:
- silently recompute and overwrite upstream decisions;
- lose objection provenance;
- promote historical model text into confirmed fact;
- erase older evidence during state transitions.

---

# X. Experts and external tools

## 20. Expert evidence does not equal adjudicative authority

Legal search, calculators, OCR, external databases, specialist models, and future regulatory agents are expert witnesses.

Their material should carry provenance, date, jurisdiction, version, retrieval scope, and confirmation state before it becomes part of the Evidence Chain.

Expertise does not automatically grant decision authority.

---

# XI. Execution

## 21. Execution must not re-adjudicate

Export, task dispatch, notification, automated document modification, and future external actions belong to the execution layer.

Execution may act only on explicitly authorized state.

It must not reinterpret, amplify, or silently replace the decision while acting.

Irreversible external actions require explicit user authorization unless a separately governed policy says otherwise.

---

# XII. Oversight — Tests / CI / Code Review / Audit

## 22. CI is a procedural safeguard

CI should protect:
- rule regressions;
- Evidence mapping;
- API schemas;
- typing and linting;
- critical frontend workflows;
- hostile-model/output boundaries;
- regression tests for confirmed historical bugs.

## 23. Green CI does not cancel a red review finding

For P1/P2 review or audit findings, the team must choose one:

1. fix the finding and add appropriate regression coverage; or
2. record an explicit **Accepted Risk** with rationale, scope, and follow-up.

The prohibited third state is:

> “We know about it, say nothing, and merge because CI is green.”

High-priority unresolved review threads should be part of merge governance.

---

# XIII. Checks and balances

## 24. No single role should own the full chain

A component should not silently own:

> investigate → validate evidence → amend law → adjudicate → execute

If implementation combines responsibilities for pragmatic reasons, interfaces and tests must preserve logical separation.

## 25. AI cannot self-authorize

Model text must never be authority for increasing model authority.

A model cannot gain power by returning:
- a new authoritative `rule_id`;
- a new candidate not admitted by the server;
- a higher severity;
- an instruction that it should directly alter status.

**Authority comes from code and governance, not model text.**

## 26. Fail-closed vs fail-soft

Governance-critical conditions should generally fail closed:
- invalid rule class;
- unknown formal state;
- forbidden authority combination;
- unverifiable critical evidence;
- structurally invalid configuration.

External or optional enrichment may fail soft:
- LLM unavailable;
- objection unavailable;
- non-critical enrichment failure.

Fail-soft must surface unavailability; it must never masquerade as “checked and safe.”

---

# XIV. Amendment and legislation

## 27. Constitutional-impact changes

The following require explicit “Constitution impact” discussion in the PR:

- AI gains direct power to alter Rule Results;
- definition of evidence truth changes;
- human confirmation boundaries are removed;
- precedence among Rule / AI / Human changes;
- audit traceability is weakened;
- runtime models may alter rules;
- critical CI / review gates are removed;
- the system may automatically perform real legal/financial external actions.

## 28. Ordinary legislation workflow

Rule or governance changes should follow:

**discover → propose → reproduce → test → implement → review → CI → merge → changelog**

AI may assist with discovery, proposals, drafts, and tests. Approval belongs to the governed process.

---

# XV. Developer execution checklist

Any PR that changes review logic should answer:

### Role
- Which institutional role is being changed?
- Does this grant new authority?

### Evidence
- Can every new material conclusion trace to exact quote / clause / span?
- Is the evidence true and relevant?
- Could evidence come from text the model did not see?
- Is coverage explicit?

### Decision
- Which field is the formal result?
- Can AI directly or indirectly overwrite it?
- Is behavior reproducible?

### Procedure
- Did graph order change?
- Are completion / unavailable / partial semantics still correct?
- Are failures being swallowed?

### Tests
- Is there regression coverage?
- Are there counterexamples, not only happy paths?
- Are duplicate, truncation, wrong-scope, and hostile-model cases covered where relevant?

### Review
- Are P1/P2 findings fixed or explicitly Accepted Risk?
- Are review threads unresolved?
- Did CI truly execute the relevant test, rather than skip it behind a feature flag?

If these questions cannot be answered, the change should not be treated as “just a small implementation detail.”

---

# XVI. Worked example

Suppose a procurement contract says:

> “Pay 95% of the contract price after equipment delivery.”

The intended process is not one model talking end-to-end:

1. **Investigation** locates payment, acceptance, technical appendix, and exceptions.
2. **Admissibility** verifies that quotations are real, correctly scoped, and visible.
3. **Judge / Rule Engine** issues the current formal status under the Rule Pack.
4. **AI counsel** may argue that acceptance is defined elsewhere or that a seven-day testing appendix was missed.
5. **Admissibility checks again** that the AI's evidence is real and relevant.
6. **Human review** decides whether to accept the objection, revise the contract, request material, or keep the original result.
7. **Case file** stores Rule Result, Evidence, Objection, Human Decision, and version.
8. **Legislature** promotes recurring discoveries into a Rule Pack PR.
9. **Oversight** adds regression tests so the system does not regress.

The goal is:

> **not unlimited AI adjudication, but coordinated intelligence under explicit authority, evidence law, and due process.**

---

# Conclusion

Agent-T begins with contract review, but the deeper experiment is more general:

> **How can generative AI participate in high-value decision systems without intelligence swallowing evidence, rules, responsibility, and procedure?**

Our answer is governance.

**Rule of Law for Agents.**

Rules do not exist to make the system less intelligent. They make intelligence auditable, correctable, trustworthy, and capable of improving without losing control.
