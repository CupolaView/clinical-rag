# clinical-rag

Retrieval-augmented question answering over Parkinson's gait and freezing-of-gait literature (PMC Open Access subset), built on AWS Bedrock and pgvector, with a published evaluation report.

**Status:** Week 1, ingest. The golden set (18 questions) was frozen on Nov 17, 2026, before any retrieval code existed.

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

Ingest the corpus (Week 1):

```bash
# 1. download the XML for all 3,660 articles (local copy needs no AWS account; the S3 copy uses your AWS profile)
python ingest/download.py --dest data/raw/v1 --limit 20                     # quick trial
python ingest/download.py --dest data/raw/v1 --dest s3://gaurav-mlops-us-west-2/clinical-rag/raw/v1

# 2. parse into section-aware chunks
python ingest/parse.py --src data/raw/v1 --out data/processed/v1/chunks.jsonl

# 3. check every golden-set source section maps to at least one chunk
python eval/check_gold_sections.py eval/golden/golden_v0.yaml data/processed/v1/chunks.jsonl
```

Both ingest scripts are safe to re-run: the download skips files already present with the right checksum.

See `docs/design-decisions.md` for scope, requirements, and evaluation targets.

**Not medical advice.** This is a literature research tool. It does not give patient-specific guidance.
