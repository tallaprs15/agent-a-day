# Sample output

Two real consecutive runs of `uv run python -m hn_digest "top stories" --memory-file .memory/sample.jsonl` against the live Hacker News front page, minutes apart, sharing one knowledge-graph file. Run 1 started with empty memory; run 2 started with the 5 stories run 1 saved. The date line shows UTC.

## Run 1 (empty memory)

Meter and checks (stderr):

```
  → fetch(url='https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30&attributesToRe...)
  → open_nodes(names=['hn-50180873', 'hn-50177916', 'hn-50174687', 'hn-50182987', 'hn-50181136', 'hn-5...)
  → create_entities(entities=[{'entityType': 'hn_story', 'observations': ['title: PHP 8.2.35', 'url: https:...)
  → create_relations(relations=[{'relationType': 'about', 'from': 'hn-50180873', 'to': 'topic-programming'},...)
— 10.8s · 5 model calls · 4/8 tool calls {'fetch': 1, 'open_nodes': 1, 'create_entities': 1, 'create_relations': 1} · 29,645 in / 1,816 out tokens
✓ 5 new stories, all links returned by a tool, none repeated (memory held 0 before), all saved.
```

Digest:

---

# Hacker News digest · 2026-10-21
## Stories     
1. **[PHP 8.2.35](https://github.com/php/php-src/releases/tag/php-8.2.35)** — PHP 8.2.35 has been officially released with bug fixes and updates. · [865 points, 482 comments](https://news.ycombinator.com/item?id=50180873).
2. **[Eight Bits of Trust: eMudhra's CA keys cracked in milliseconds](https://scotthelme.co.uk/eight-bits-of-trust-emudhras-ca-keys-cracked-in-milliseconds/)** — Security analysis reveals that eMudhra's certificate authority keys could be cracked in milliseconds due to weak trust assumptions. · [700 points, 344 comments](https://news.ycombinator.com/item?id=50177916).
3. **[Muse exposes users' personal and business secrets when they share conversations](https://arstechnica.com/security/2026/10/meta-muse-exposes-users-personal-and-business-secrets-when-they-share-conversations/)** — A security flaw in Meta's Muse exposes sensitive personal and business secrets when users share conversations. · [365 points, 153 comments](https://news.ycombinator.com/item?id=50174687).
4. **[Today's PHP security release, and how we're strengthening security](https://thephp.foundation/blog/2026/10/21/todays-php-security-release-and-how-were-strengthening-php-security/)** — The PHP Foundation details today's security release and outlines ongoing efforts to strengthen ecosystem security. · [334 points, 161 comments](https://news.ycombinator.com/item?id=50182987).
5. **[DuckDB 2.0.0 LTS “Cinnamon Teal”](https://duckdb.org/2026/10/21/announcing-duckdb-200)** — DuckDB announces version 2.0.0 LTS, codenamed "Cinnamon Teal", bringing significant new features and long-term support. · [273 points, 29 comments](https://news.ycombinator.com/item?id=50181136).

## Memory      
0 candidates were already covered and skipped, 5 new stories saved.

---

## Run 2 (memory holds run 1's stories)

Meter and checks (stderr):

```
  → fetch(url='https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30&attributesToRe...)
  → open_nodes(names=['hn-50180873', 'hn-50177916', 'hn-50174687', 'hn-50182987', 'hn-50181136', 'hn-5...)
  → open_nodes(names=['hn-50181114', 'hn-50184586', 'hn-50181175', 'hn-50150214', 'hn-50149861', 'hn-5...)
  → create_entities(entities=[{'entityType': 'hn_story', 'observations': ['title: Compromised LiteLLM 1.107...)
  → create_relations(relations=[{'relationType': 'about', 'from': 'hn-50181114', 'to': 'topic-security'}, {'...)
— 12.3s · 6 model calls · 5/8 tool calls {'fetch': 1, 'open_nodes': 2, 'create_entities': 1, 'create_relations': 1} · 41,781 in / 1,804 out tokens
✓ 5 new stories, all links returned by a tool, none repeated (memory held 5 before), all saved.
```

Digest:

---

# Hacker News digest · 2026-10-21
## Stories     
1. **[Compromised LiteLLM 1.107.0 on PyPI Installs a Credential Stealer and Backdoor](https://safedep.io/litellm-pypi-compromise-credential-stealer-backdoor/)** — A malicious package version uploaded to PyPI installs credential stealers and backdoors, posing risks to users. · [264 points, 98 comments](https://news.ycombinator.com/item?id=50181114).
2. **[EA is shutting down BioWare](https://www.theverge.com/games/1015647/ea-is-shutting-down-bioware)** — Electronic Arts has announced the closure of legendary game developer BioWare. · [191 points, 112 comments](https://news.ycombinator.com/item?id=50184586).
3. **[Patch the Planet: global response coordination as agents uncover vulnerabilities](https://openai.com/index/coordinated-disclosure-of-critical-vulnerabilities/)** — OpenAI explores global response coordination as autonomous AI agents help uncover critical vulnerabilities. · [178 points, 108 comments](https://news.ycombinator.com/item?id=50181175).
4. **[Way to Play Super Mario 64: On Apple Vision Pro](https://www.pagetable.com/303)** — A new walkthrough and method details how to get Super Mario 64 running on the Apple Vision Pro. · [172 points, 66 comments](https://news.ycombinator.com/item?id=50150214).
5. **[The Decoherence Trap](https://maevyn.codes/decoherence)** — An exploration of technical challenges and concepts surrounding decoherence. · [163 points, 69 comments](https://news.ycombinator.com/item?id=50149861).

## Memory      
5 candidates were already covered and skipped; 5 new stories were saved.

---
