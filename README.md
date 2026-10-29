# clinical-rag

Retrieval-augmented question answering over Parkinson's gait and freezing-of-gait literature (PMC Open Access subset), built on AWS Bedrock and pgvector, with a published evaluation report.

**Status:** Week 0, design and golden set. No pipeline code yet, on purpose: the eval questions are written before the retriever exists.

| Folder | Purpose |
|---|---|
| `ingest/` | select → download → parse JATS XML → section-aware chunks |
| `index/` | Titan v2 embeddings → RDS PostgreSQL + pgvector |
| `retrieval/` | vector, lexical, fusion, rerank |
| `generation/` | prompts, citation enforcement, refusal |
| `eval/` | golden set, validator, RAGAS harness, `results/` |
| `api/`, `ui/` | FastAPI and Streamlit (January) |
| `docs/` | design decisions, eval report, metric changelog |

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in locally; never commit .env
python eval/validate_golden.py eval/golden/golden_v0.yaml --strict
pytest
```

Build the frozen corpus manifest (once; needs no AWS account, the PMC bucket is public):

```bash
python ingest/build_manifest.py --limit 50 --out data/manifest_trial.csv   # 1-minute trial
python ingest/build_manifest.py                                             # full run → ingest/manifest_v1.csv
```

See `docs/design-decisions.md` for scope, requirements, and evaluation targets.

**Not medical advice.** This is a literature research tool. It does not give patient-specific guidance.
