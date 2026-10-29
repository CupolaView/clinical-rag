# Clinical RAG: Design Doc (v0, Week 0)

*Status: DRAFT, written Oct 2026 before any pipeline code. Decisions marked **[decide]** are still open.*

## 1. Problem

Clinicians and researchers working on Parkinson's gait problems have to read across hundreds of papers to answer narrow questions. Examples: "What cueing modalities reduce freezing-of-gait episodes?" or "What sensor placements do wearable FoG detectors use?" General-purpose LLMs answer these fluently but cite poorly and hallucinate numbers. This system answers literature questions **only from a fixed, citable corpus** and says so when the corpus does not contain the answer.

## 2. Users and requirements

**User:** a clinician or researcher asking literature questions. This is not a patient tool.

| # | Requirement | Why it matters later |
|---|---|---|
| R1 | Every factual sentence in an answer cites `[PMCID §section]` from retrieved passages | Drives citation-forced prompting and the faithfulness metric |
| R2 | When retrieved context does not support an answer, the system says so instead of guessing | Tested by the unanswerable questions in the golden set |
| R3 | **No patient-specific advice.** Questions about an individual's diagnosis, dosing, or treatment are declined with a pointer to a clinician | Drives the Bedrock Guardrails policy (January) |
| R4 | Every answer shows the source passages it used | Lets the user verify, and makes eval debugging possible |
| R5 | p95 latency and cost per query are measured and published | README requirement in the Phase 2 exit gate |

## 3. Scope

**Corpus:** PMC Open Access subset, filtered to Parkinson's gait, freezing of gait (FoG), and gait rehabilitation. Target **2,000–5,000 full-text articles**: large enough that retrieval is genuinely hard, small enough to embed for a few dollars.

- **Source:** the PMC Cloud Service (`s3://pmc-oa-opendata`, us-east-1, no sign-in required). The legacy PMC FTP bulk packages were retired in August 2026.
- **Format:** JATS XML rather than plain text, because section-aware chunking needs `<sec>` boundaries, titles, and tables.
- **Selection:** a PMC search query produces the PMCID list, which is frozen as `ingest/manifest_v1.csv`. **Decided Oct 29, 2026:**
  `parkinson*[tiab] AND (gait[tiab] OR "freezing of gait"[tiab] OR walking[tiab]) AND open access[filter]`
  This returned **3,667 articles** on Oct 29, 2026. Terms are restricted to title and abstract (`[tiab]`) because PMC otherwise matches the full text, where "gait" appears in passing in tens of thousands of papers. The first draft query returned 76,461 for that reason. A spot check of 25 results found about 70% squarely on topic; the rest mention gait incidentally (e.g. MSA, case reports), and are kept as realistic distractors.
  - Fallback if gait-rehabilitation coverage turns out thin: add `OR cueing[tiab] OR treadmill[tiab] OR "physical therapy"[tiab] OR physiotherapy[tiab] OR rehabilitation[tiab]` inside the second group (4,641 articles).
  - The count drifts as PMC adds papers, so the manifest is frozen once, not re-queried.
- **Pre-check of all 3,667 hits against the bucket (Oct 29, 2026):** every hit has a folder in `s3://pmc-oa-opendata`, and every one has JATS XML. 74 have more than one version, and `build_manifest.py` takes the latest. 5 are flagged retracted and 2 are flagged not open access; these are kept in the manifest with a `status` column for audit but excluded from the corpus. **Expected corpus: 3,660 articles.**
- **Licenses across all hits:** CC BY 2,744 · CC BY-NC 405 · CC BY-NC-ND 413 · CC BY-NC-SA 55 · CC BY-ND 7 · CC0 3 · TDM 3 · not recorded 37. About 24% are non-commercial. **[decide]** keep them (a non-commercial portfolio demo) or filter to CC BY/CC0 (about 2,750 articles, still in range).
- **License:** store each article's license in the manifest and show it in the UI. **[decide]** include non-commercial (CC BY-NC) articles. They are fine for a public non-commercial portfolio demo, but excluding them shrinks the corpus.
- **Versioning:** the manifest and processed chunks are versioned (DVC or S3 prefixes, as in Project 1). The corpus is frozen for the whole eval period so metric changes reflect pipeline changes, not data changes.

**Non-goals:** chat memory or multi-turn context; agents or tool use (Project 4); fine-tuning (Project 3); real-time corpus updates; non-English papers; figures and images (captions are indexed, pixels are not).

## 4. Architecture (target by Nov 29)

```
manifest_v1.csv → download JATS XML (S3 → S3, us-east-1 → us-west-2)
  → parse + section-aware chunking (ingest/)
  → Titan Text Embeddings v2 → RDS PostgreSQL + pgvector (index/)
  → hybrid retrieval: Postgres full-text (BM25-style) + vector, fused with RRF (retrieval/)
  → Cohere Rerank 3.5 on Bedrock → top-k passages
  → Claude Haiku 4.5 on Bedrock, citation-forced prompt + refusal rule (generation/)
  → answer + citations + source passages
```

All Bedrock calls run in **us-west-2**. Model IDs were smoke-tested on Oct 26 and are listed in `.env.example`.

**Open design choices to decide with data, not upfront:**

- Chunk size and overlap. Start at about 400 tokens with section-title prefixing, then measure with the eval harness.
- Titan v2 embedding dimension: 1024, 512, or 256. Smaller dimensions are cheaper; measure the recall cost.
- Fusion top-k before rerank (start with 50) and after rerank (start with 8).
- Table handling: flatten tables to text, or index them as separate chunks.

## 5. Evaluation

- **Golden set:** at least 20 questions written in Week 0, **before any retrieval code exists**, then grown to 50–100 by Jan 10. Each question carries type tags (factual, comparative, numeric/table, multi-paper), and some are deliberately unanswerable. Schema is in `eval/golden/`.
- **Retrieval metrics:** recall@k and MRR against the gold PMCIDs and sections. These need no LLM judge, so they are cheap to run on every change.
- **End-to-end metrics (RAGAS):** faithfulness > 0.9, answer relevancy > 0.85, context precision > 0.8.
- **Refusal metrics:** refusal rate on unanswerable questions (target 100%) and false-refusal rate on answerable ones.
- **Discipline:** every pipeline change gets a row in `docs/metric-changelog.md` recording the change, the before and after metrics, and whether it was kept.

## 6. Cost and operations guardrails

- AWS budget alert in place before the first embedding run.
- Stop the RDS instance when idle. AWS restarts a stopped instance automatically after 7 days, so re-stop it or snapshot and delete it.
- Embed the corpus once per corpus version, and cache embeddings in S3 so re-indexing does not re-pay for them.

## 7. Milestones

| Date | Gate |
|---|---|
| Nov 1 | This doc + 20 golden questions committed (the git timestamp proves they predate the retriever) |
| Nov 15 | Corpus ingested, embedded, and queryable |
| Nov 29 | End-to-end: question in, cited answer out |
| Dec 13 | Golden set expanded + RAGAS baseline in CI |
| Jan 10 | Eval targets met, or a documented reason why not |
| Jan 31 | Live demo, eval report, writeup (Phase 2 exit) |
