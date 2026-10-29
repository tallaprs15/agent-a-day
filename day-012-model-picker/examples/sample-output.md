# Model shortlist
> Multilingual sentence embeddings for semantic search, permissive licence, small enough for a laptop CPU

## Ranking
| # | Model | Total | task fit | constraint fit | popularity | licence | freshness |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | [sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) | **98.0** | 5 | 5 | 5 | 5 | 4 |
| 2 | [sentence-transformers/distiluse-base-multilingual-cased-v2](https://huggingface.co/sentence-transformers/distiluse-base-multilingual-cased-v2) | **91.0** | 5 | 4 | 5 | 5 | 3 |
| 3 | [sentence-transformers/paraphrase-multilingual-mpnet-base-v2](https://huggingface.co/sentence-transformers/paraphrase-multilingual-mpnet-base-v2) | **91.0** | 5 | 4 | 5 | 5 | 3 |
| 4 | [sentence-transformers/clip-ViT-B-32-multilingual-v1](https://huggingface.co/sentence-transformers/clip-ViT-B-32-multilingual-v1) | **77.0** | 3 | 4 | 5 | 5 | 3 |

Scores are 0-5 per criterion; total is 0-100. task fit 35% (model), constraint fit 25% (model), popularity 15% (computed from Hub data), licence 15% (computed from Hub data), freshness 10% (computed from Hub data).

## Why
### 1. sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
_661,000,000 downloads, 1,495 likes, licence apache-2.0, updated 2026-01-28_

This model is specifically designed for multilingual sentence embeddings and semantic similarity tasks, with an Apache-2.0 permissive licence. Its 117.7M parameters make it lightweight and well-suited for a laptop CPU.

### 2. sentence-transformers/distiluse-base-multilingual-cased-v2
_49,000,000 downloads, 220 likes, licence apache-2.0, updated 2025-03-06_

This model provides multilingual sentence embeddings with a permissive Apache-2.0 licence and a moderate size of 134.7M parameters. It runs reasonably well on a laptop CPU.

### 3. sentence-transformers/paraphrase-multilingual-mpnet-base-v2
_145,400,000 downloads, 554 likes, licence apache-2.0, updated 2025-08-19_

It matches the multilingual sentence similarity task and has a permissive Apache-2.0 licence. However, at 278.0M parameters, it is larger and less ideal for a laptop CPU compared to smaller alternatives.

### 4. sentence-transformers/clip-ViT-B-32-multilingual-v1
_5,900,000 downloads, 195 likes, licence apache-2.0, updated 2024-11-05_

While it features a permissive Apache-2.0 licence and 134.7M parameters suitable for a laptop CPU, it is primarily a multimodal CLIP model rather than a pure text sentence embedding model.

**Caveat:** Multimodal architecture might not align strictly with pure text semantic search.
