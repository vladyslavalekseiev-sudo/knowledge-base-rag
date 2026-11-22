# Relay Knowledge — QA report

**Date:** 2026-11-22, Europe/Berlin. **Scope:** application, tests, dependencies and deployment configuration. **Decision:** suitable for a local portfolio demonstration; **not ready for unrestricted production use**.

The application works on the checked corpus, handles the tested failure modes, and has substantially stronger regression coverage. A real retrieval failure remains. Container execution, other operating systems, public hosting, and sustained production operation remain unverified. No P0 defect was confirmed. No claim of universal answer correctness or absence of all vulnerabilities is made.

## Scope and environment

Reviewed all 29 baseline tracked files: application modules, tests, evaluation code and datasets/results, five Markdown documents, browser assets, README, dependency files, Git ignore rules, Dockerfile and CI workflow. New application/test/tooling files were reviewed and linted as well. The existing UI image was not treated as executable code or test evidence; new screenshots were taken from the running application.

- Windows 10 build 19045; Python 3.13.13; Node 26.6.0.
- Fresh isolated environment at `.data/qa-venv`, created from the existing Python installation with system site-packages disabled. Dependencies installed from the declared requirements, not copied from the old environment.
- FastAPI 0.163.1, Starlette 1.10.0, Uvicorn 0.35.0; resolved Pydantic 2.14.4. Full audited versions: [dependencies.json](docs/qa/dependencies.json).
- Real optional model: Qwen 2.5 0.5B via the existing local Ollama 0.40.16 runtime, CPU inference, cloud disabled.
- TestClient integration tests used temporary databases. Model protocol fault tests explicitly used simulated local HTTP servers; they are not counted as real Qwen inference. Qwen was tested separately.
- Browser checks used a real separate Uvicorn process and index. Stress tests spawned and stopped their own process tree and disposable index.

The first load measurement followed a Windows Python launcher rather than the worker. Those CPU/RAM/recovery measurements were discarded, the harness was fixed, and the entire load run was repeated.

## Architecture assessment

`documents` validates Markdown and chunks sections; `search` builds and queries a derived SQLite FTS5 index; `answer` requires evidence for each question clause and optionally narrows quotes through Ollama; `app` validates HTTP contracts and serves the UI. The browser renders strings through text nodes, independently fetches source passages, and handles failures. New ASGI middleware bounds question bodies and adds browser protections.

The layers have clear dependencies and no observed circular imports. Database reads use separate read-only connections; parameters are bound rather than interpolated into SQL. Index construction uses a temporary file and atomic replacement. Tests confirmed that a failed rebuild retains the previous usable database. Markdown remains the source of truth; the SQLite index is disposable. There is no conversation-history feature to persist or recover. Citation IDs are tied to document/section/chunk positions and can change when the corpus is edited.

No meaningful dead application code or duplicate business logic was identified by review and Ruff. Unused imports introduced during QA development were removed. Similar response-building code for source references is already shared. Static checks are evidence of the configured rules, not a formal proof of code quality.

The retriever is English lexical matching with hand-written normalization and scope checks. It is **not embedding-based semantic retrieval**. Exact source quotation prevents invented quotation text, but neither quotation identity nor structural sentence-range validation proves that the selected content fully answers the question.

## Results and counts

| Suite | Executed | Passed | Failed | Evidence |
|---|---:|---:|---:|---|
| Python unit/integration/boundary/negative/regression tests | 92 | 92 | 0 | [tests.json](docs/qa/tests.json) |
| Offline retrieval regression questions | 29 | 29 | 0 | [regression.json](docs/qa/regression.json) |
| Real Qwen/Ollama regression questions | 29 | 29 | 0 | [model.json](docs/qa/model.json) |
| Harder retrieval challenge questions | 8 | 7 | **1** | [challenge.json](docs/qa/challenge.json) |
| Executed browser scenarios | 20 | 20 | 0 | [browser-checks.md](docs/qa/browser-checks.md) |
| Real HTTP JSON/media boundary probes | 134 | 134 | 0 | [http-boundaries.json](docs/qa/http-boundaries.json) |

These are different kinds of checks, so load repetitions are not added to a misleading single test-case total. The table contains 312 scenario executions, 311 passed and one failed; the two 29-question runs intentionally reuse the same questions in different modes. Browser scenarios were executed through automation, but are documented replay steps rather than a checked-in standalone browser test runner. The HTTP probes include 128 seeded random malformed byte bodies, five explicit hostile/media cases and one valid search; all ran against a real localhost server. The JSON file preserves statuses from the confirmed pre-fix HTTP 500 reproductions as well.

**Coverage:** `rag/` has 387/387 executable statements and 118/118 branches covered: **100% application Python statement and branch coverage**. [Coverage evidence](docs/qa/coverage.json). This excludes JavaScript, evaluation tools, configuration, model reasoning, untested platforms and the space of possible user questions. It is not 100% project coverage or a quality score for generated answers.

`pytest -W error` passed, so no warning was suppressed to obtain the result. Ruff, `node --check web/app.js`, `actionlint` and `git diff --check` passed. `pip check` found no broken requirements. The final full isolated-environment `pip-audit` completed with no known vulnerabilities reported across its 52 installed packages; that includes application, development and QA dependencies. This is a point-in-time package-advisory result, not an operating-system/container-image audit.

Before fixes, seven assertions in the expanded run failed for the body limit, browser headers, database health, and title-only documents. A separate real HTTP test also reproduced model queue overload. The initial run additionally encountered a QA-harness problem: a huge parameter-derived test ID exceeded Windows' environment-variable limit. Explicit short IDs fixed the harness; all fault cases were then actually executed. A leaked SQLite handle in a new test was fixed with explicit connection closing. Neither harness issue was presented as a product defect.

## Confirmed findings and fixes

| ID | Priority | Cause and impact | Resolution / verification |
|---|---|---|---|
| QA-01 | P1 under public exposure | Request validation limited question characters only after reading/parsing the entire body. A 100 KB invalid field was echoed by validation; much larger bodies could consume memory. | Bound `/api/ask` bodies to 8192 bytes before JSON parsing and return small HTTP 413 responses. Tests cover known-length, streamed, fragmented, exact-limit and disconnected requests. Check happens before copying the next body chunk into the buffer. |
| QA-02 | P2 | Health returned cached startup statistics even if the index disappeared or was corrupt. Searches could then return generic 500 errors. | Lightweight real read probe; missing/corrupt/schema-less/empty indexes are unavailable. Known SQLite failures return HTTP 503 without database paths. Restart reconstructs the index. All three damaged-file/schema cases and empty readiness are tested. |
| QA-03 | P2 | A Markdown file with only a valid title was silently ignored when other valid files existed. | Reject every document without a section. Also tested empty text/sections, invalid and duplicate identifiers, duplicate sections, missing corpus and chunk boundaries. |
| QA-04 | P2 | Original pytest 8.4.2 has a known Unix temporary-directory advisory. The newly created environment inherited pip 26.0.1 with four distinct advisory IDs. Scanner returned duplicate records for some advisories. | Pin pytest 9.0.3; upgrade pip to 26.3 in the isolated QA environment and local/CI/container installation instructions. Before evidence is [security-before.json](docs/qa/security-before.json). Full repeat audit is clean. The old pre-existing `.venv` was not silently rewritten; reinstall/update using the README instructions. |
| QA-05 | P2 | Invalid `KB_OLLAMA_URL` could survive startup and fail while constructing a model request; credentials/query strings also encouraged unsafe configuration. | Validate HTTP(S) origin at construction and reject missing host, embedded credentials, query or fragment. Six invalid configurations are tested. This is operator configuration; user questions cannot set the destination. |
| QA-06 | P2 | Slow model calls had no application admission limit and could occupy the synchronous request worker pool. | At most two simultaneous calls per Ollama instance/application process; overloaded requests immediately fall back to indexed excerpts. A real local blocking HTTP service verifies admission, prompt fallback and slot reuse. A final real Qwen run also passes. |
| QA-07 | P3 | Homepage lacked defense-in-depth browser headers. No successful XSS exploit was demonstrated. | Added restrictive homepage CSP, `nosniff`, `DENY` framing and no-referrer policy. Browser app still works; source/answer strings use text rendering. API reference is intentionally outside the homepage CSP because its default UI uses external CDN resources. |
| QA-08 | P2 | Deep JSON, escaped unpaired Unicode surrogates and `NaN` each caused real HTTP 500 responses. Decoder recursion and error-response serialization were not safe for these values. A deeply nested model response could also escape fallback. | Validate bounded JSON text, finite values, valid Unicode and depth before FastAPI handling; return compact HTTP 400. Reject unsupported content types with HTTP 415 before bytes can enter schema error serialization. Catch recursive model decoding failures and fall back. Seven hostile-input regressions plus a deep-response fault test pass; the live 134-case HTTP probe has no unexpected status or 500. |

P0 = critical, P1 = high, P2 = medium, P3 = low. No substantial architecture change, new hosted dependency, authentication system or retrieval-engine replacement was introduced.

## Functional and failure coverage

Real checks covered startup, static assets, schema/API reference, every application API route, supported and unsupported questions, multiple independent sources, source reopening, malformed/extra/wrong-type JSON fields, 2/3/360/361-character boundaries, whitespace, Unicode, control characters, SQL/HTML-shaped input, unsupported methods and missing paths. Source IDs are parameterized SQL values; traversal-shaped paths did not expose application files. Cross-origin requests receive no permissive CORS header.

The corpus tests cover malformed headings, empty and duplicate sections/documents, missing corpus, sentence and unpunctuated long chunks, and preservation of all words at up to 5000 words. The database tests cover persistence through a new Index instance, failed replacement, clean rebuild, corruption and restart recovery. Concurrent readers returned deterministic answers across 160 repeated direct calls.

Model fault tests explicitly use local HTTP fault servers: non-JSON, wrong JSON type, HTTP 500, truncated response, oversized response, malformed message content, missing fields and explicit refusal. A real refused TCP connection verifies offline fallback. The application keeps its 20-second model socket timeout and 64 KiB response-read ceiling; a separate full-duration 20-second timeout/trickle-response test was **not** run. Malformed numeric ranges, boolean numbers, out-of-range/reversed/duplicate ranges, source order and scope preservation are tested. Explicit model refusals are preserved.

The real model run passed 29/29, including 8/8 expected refusals. Of the 21 answered questions, 20 used model-selected excerpts and one used extractive fallback. Median per-question latency was **2486.67 ms**; the offline regression median was **2.07 ms**. These engine timings exclude browser/network overhead and describe this small corpus and machine only.

Browser scenarios cover all three examples, manual compound questions, both citation link placements, dialog Close/Escape, focus restoration, direct and missing-source links, minimum/empty input, actual server outage/retry, API-reference/navigation links and an HTML-shaped input. Screenshots were inspected at three viewport sizes. Labels, headings, status region and dialog semantics were checked. No independent screen-reader, axe or formal WCAG audit was run. There are no upload/settings/authentication menus in this application to exercise.

## Performance and bounded stress results

[Verified performance evidence](docs/qa/performance.json); reproducible tool: `python -m evaluation.stress --soak-seconds 180`.

| Concurrency | HTTP requests | Errors | Requests/s | p50 | p95 | p99 |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 100 | 0 | 130.53 | 6.11 ms | 23.51 ms | 32.69 ms |
| 8 | 400 | 0 | 296.97 | 25.35 ms | 41.52 ms | 46.99 ms |
| 32 | 1000 | 0 | 292.54 | 105.89 ms | 133.87 ms | 158.20 ms |

The separate 180.07-second soak performed **11,168 additional HTTP searches**, zero errors, p95 31.65 ms. Total measured load/soak searches: **12,668**, all returned the expected source and answer text. Server worker RSS: 60.71 MB at start, 57.75 MB at end, 67.67 MB peak. OS handles changed from 176 to 176. Worker CPU time during soak was 65.34 seconds, approximately 36% of one logical core averaged over the interval. This short run did not demonstrate a persistent leak; it cannot exclude a slow leak over hours/days. Load was generated on the same machine without TLS or a reverse proxy, so figures are not production capacity guarantees.

Cold application readiness took 2213.55 ms. The tool killed its own full process tree, verified the old worker was gone, overwrote only its disposable derived database, and restarted successfully in 2125.04 ms. Source retrieval and a correctly answered question were checked after startup/recovery.

| Synthetic passages | Build | Search p50 | Last-ID source lookup | SQLite size |
|---:|---:|---:|---:|---:|
| 1000 | 71.45 ms | 7.65 ms | 2.60 ms | 327,680 bytes |
| 10,000 | 287.92 ms | 36.94 ms | 10.29 ms | 2,949,120 bytes |

The scaling corpus deliberately repeats common search terms with unique IDs; it is a stress workload, not a semantic-retrieval benchmark. FTS candidate sorting and the unindexed source-ID lookup become visible costs. These are acceptable at the checked demo size but should be reconsidered for a large library. No connection cache, speculative memoization or extra database service was added without evidence of need.

## Security assessment

No passwords, tokens or API keys were found in the reviewed tracked configuration/code. The app needs no key in offline/local Ollama modes; `.env` and model/database/runtime directories are ignored by Git. Questions and retrieved passages are sent only to the operator-configured Ollama endpoint. That endpoint must be trusted; choosing a remote endpoint changes the data boundary. No internet service received document contents during these checks.

SQL parameters, FTS query tokenization, read-only request connections, text-only DOM construction, bounded request bodies, schema validation and generic database errors address the tested injection and data-exposure paths. There is no application shell execution or upload endpoint. There are no state-changing business endpoints or credential cookies; conventional CSRF account-action scenarios do not apply to this sample app.

The application intentionally has no login or document ACLs. Every served document is readable to every visitor. It is safe to demonstrate only with the fictional/public corpus under the current access model. Public deployment still needs HTTPS, request/connection rate limits and operational controls. The two-call model guard is not a distributed rate limiter. CSP does not replace authentication, input validation or protection of private documents.

References used to check security expectations and advisories: [OWASP REST Security](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html), [OWASP CSP](https://cheatsheetseries.owasp.org/cheatsheets/Content_Security_Policy_Cheat_Sheet.html), [pip-audit](https://github.com/pypa/pip-audit), [pytest advisory](https://github.com/advisories/GHSA-6w46-j5rx-g56g), [pip URL-handling advisory](https://github.com/advisories/GHSA-qwm4-qh6w-59xr).

## Remaining risks and checks not performed

| Priority | Item | Practical implication / next action |
|---|---|---|
| P2 | Challenge-01 still falsely refuses `Can the owner stop a pending workspace deletion, and how long is the cancellation window?` | The second clause needs reference resolution and different wording. It is not hidden or removed from the dataset. Plan a separately reviewed retrieval improvement and expand genuinely held-out questions. |
| P2 | Semantic completeness of a selected quotation is not guaranteed | Structural/source/scope checks cannot prove the quotation answers every intent or includes every restriction. Larger independent evaluations and adversarial model selection tests are needed before operational reliance. |
| P1 for private/internet use | No authentication, document ACLs, HTTPS termination or distributed rate limiting | Sensitive documents and unrestricted expensive model traffic require an access-control model and operational limits before deployment. |
| P2 release verification | Docker is absent; no WSL distribution/container daemon is available. Linux/macOS and GitHub-hosted CI were not executed. | Dockerfile/workflow were inspected and workflow syntax checked, but a real image build/start and OS compatibility remain release gates. No public deployment was attempted. |
| P2 operational verification | Three-minute soak; no 24-hour run, disk-full test, host reboot, multiworker replacement stress or production-like network proxy | Do not infer long-term availability, total timeout behaviour or leak freedom from this bounded run. |
| P3 | ID lookup scans an FTS unindexed column; broad FTS queries sort growing candidate sets | For larger corpora, profile a primary-key passage lookup alongside FTS and evaluate ranking scalability. |
| P3 | Transitive dependencies and base image are not fully locked to hashes/digests | The captured dependency snapshot is reproducible evidence of this run; future fresh resolutions/images may differ. Add lock/hash generation and image scanning for a release workflow. |
| P3 compatibility/accessibility | One browser engine and viewport emulation only | Real Firefox/Safari, physical mobile devices and assistive technology still need checks. API reference needs external CDN assets on a cold load. |

## Reproduce and release decision

From a new virtual environment, upgrade pip using the README, install `requirements-qa.txt`, then run:

```bash
python -m pytest -W error -q --cov=rag --cov-branch --cov-fail-under=90
python -m ruff check rag tests evaluation
python -m pip_audit
node --check web/app.js
python -m evaluation.run
python -m evaluation.run --cases evaluation/challenge.json
python -m evaluation.stress --soak-seconds 180 --output .data/qa/performance.json
# With an existing local Ollama server/model:
python -m evaluation.run --model qwen2.5:0.5b
```

The challenge command intentionally exits nonzero for the unresolved failure. `actionlint .github/workflows/ci.yml` checks workflow syntax if actionlint is installed. CI now runs the expanded Python checks, coverage threshold, dependency audit and existing container startup check; the updated hosted workflow itself has not been run in this environment.

**GO:** present the source and locally demonstrate the fictional knowledge base, source-linked answers, refusal behaviour and actual QA evidence in a GitHub portfolio.

**NO-GO:** production use with private/company documents until the release gates above are closed. Container execution and multiplatform deployment still require verification.
