# Logistics KPI Narrative Summary Agent — Test Specification

**Template ID:** LOG-C2-018
**Test framework:** pytest

---

## 1. Overview

| Aspect | Value |
|---|---|
| Template ID | LOG-C2-018 |
| Pattern × Industry | Narrative summary × LOG (Logistics) |
| Category / Base | Cat 2 / `AgentBaseGraph` — direct framework inheritance |
| Model access | No network in any tier. The model path is exercised through a stand-in exposing the framework's client contract; the deterministic path needs no model at all. |

Three tiers, each answering a different question. The unit tier asks whether the rules
are right. The boundary tier asks whether the architecture holds. The integration tier
asks whether the thing works when a caller actually calls it — and it is the tier that
matters most here, because a pipeline can pass every unit test while producing nothing
through its public entry point.

---

## 2. Unit tier — `tests/unit/`

### 2.1 Caller-data contract — `test_input_validate.py`

Every case calls `execute()` directly rather than through the framework wrapper: the
template's guarantees have to hold where that wrapper is absent or configured
differently, so a test proving only "the framework refused it" proves nothing about
this template.

| ID | Scope | Expected |
|---|---|---|
| TC-01 | Accepted payloads — full, baseline-free, audience-defaulted, JSON-encoded | Validated figures written as sorted JSON strings; audience defaults to `executive` |
| TC-02 | The raw parameter mapping after validation | Cleared, so it cannot reach the checkpoint or the response |
| TC-03 | Non-finite matrix, per field, both payloads: `"NaN"`, `"Infinity"`, `"-Infinity"`, raw `nan`, raw `inf`, raw `-inf`, over-magnitude, under-magnitude | Refused, naming the field |
| TC-04 | Booleans and non-numeric strings as KPI values | Refused — `isinstance(True, int)` means a bare numeric check would accept `true` as 1 |
| TC-05 | Bare `NaN` arriving in raw JSON rather than as a string | Refused |
| TC-06 | Structural limits — absent required payload, missing declared field, unknown parameter, over-cap entries, over-cap parameters, non-object payload, malformed JSON, unknown audience | Refused; an unknown parameter's name is never echoed back |
| TC-07 | Inert field names — spaces, hyphens, dots, over-length, punctuation, full-width | Refused |
| TC-08 | Ordinary domain field names (`dock_to_stock_hours`, `pick_accuracy_pct`, `co2_kg_per_tkm`) | Accepted. The fail-closed direction is the one that stops real reports being produced. |
| TC-09 | Instruction screen — `<|im_start|>`, `[INST]`, `<<SYS>>`, a directive phrase, a tag-spliced directive, a "new system instructions" preamble | Refused as instruction-shaped |
| TC-10 | A hostile parameter *name*, and an escaped payload | Refused; escaping is not an evasion because the scan runs after parsing |
| TC-11 | Real logistics sentences that resemble attack phrases ("the carrier will act as a customs agent", "insert into the manifest…") | Not refused |
| TC-12 | Driver references — hashed with a salt, dropped without one, short salt treated as absent, non-inert reference refused, digest stable and salt-dependent | As stated |

### 2.2 Audience resolution — `test_intent_classify.py`

| ID | Scope | Expected |
|---|---|---|
| TC-13 | Each configured audience | Correct tone, ceiling and section list; serialisation stable across runs |
| TC-14 | Unknown, absent, non-string audience | Refused |
| TC-15 | Malformed bundles at construction | Rejected when the node is built, not on the first request |

### 2.3 Comparison and narrative — `test_response_generate.py`

| ID | Scope | Expected |
|---|---|---|
| TC-16 | Direction table across three KPIs × three severities | A movement in the good direction is always normal; the bad direction is banded. A falling unit cost is an improvement; a falling on-time rate is not. |
| TC-17 | A movement sitting exactly on a band | Classified into the band, not below it — the epsilon absorbs the noise of subtracting two near-equal ratios |
| TC-18 | Absent baseline field, and a KPI with no configured band | Reported as unknown, never as zero |
| TC-19 | Deterministic writer — report produced with no model, reflecting the caller's figures, following the audience's section list | As stated |
| TC-20 | A period with no breach | Says so, rather than inventing an exception |
| TC-21 | Exception ranking | Most severe first, capped at three |
| TC-22 | Model path — called once, both message roles, prompt carries the resolved comparison | As stated |
| TC-23 | Unusable model responses (empty, blank, null, wrong shape, oversized) | Refused |
| TC-24 | Missing upstream inputs | Refused before the model is called, so no request is paid for |
| TC-25 | Construction — client without the expected method, non-finite band, unknown direction | Rejected at construction |

### 2.4 Release boundary — `test_output_validate.py`

| ID | Scope | Expected |
|---|---|---|
| TC-26 | Clean report | Released; caller figures cleared |
| TC-27 | Missing disclaimer | Appended, case-insensitively matched, configurable |
| TC-28 | Every credential shape the framework detector recognises — Stripe, generic API key, JWT, AWS key id, bearer token, database and cache connection strings | Withheld. This table is the framework's block set, not an approximation of it. |
| TC-29 | Individual attribution — operational reference, Japanese and Latin name attributions, anonymised digest | Withheld |
| TC-30 | Ordinary report language naming carriers, depots and roles without individuals | Released |
| TC-31 | Containment — every field the node can write is overwritten; the placeholder is non-empty; neither the refused text nor the matched value appears anywhere in the update; the refusal returns rather than raises | As stated |

### 2.5 State contract — `test_state_contract.py`

| ID | Scope | Expected |
|---|---|---|
| TC-32 | The state module extends the framework state and does not shadow it | AST inspection |
| TC-33 | The graph declares its own schema | A graph still declaring the framework base drops every domain field between nodes |
| TC-34 | Every field the pipeline writes is declared in the schema | An undeclared field is silently dropped by the graph runtime |

### 2.6 Framework compliance — `test_framework_compliance_tc06_tc07.py`

Replacing either of the framework's own security gates raises at class-definition time.
Domain checks attach through the extension hooks instead.

---

## 3. Boundary tier — `tests/proof_of_boundary/`

| ID | Boundary | Expected |
|---|---|---|
| PB-1 | Import isolation (`test_import_isolation.py`) | No platform-SDK import, no abolished-tier import, no deprecated framework shim, no relative import escaping the package |
| PB-2 | Backbone order through a full invoke (`test_pb_invoke_order.py`) | The recorded node history matches the fixed backbone; a refusal short-circuits before the release slot; the release slot is the last domain node |
| PB-3 | Sign-off payload | The request pinned in this test is byte-equal to `deploy/invoke_payload.json` and carries the domain parameters, so the run proved here and the run the deployment check performs are the same run |
| PB-4 | Trust gate (`test_pb_invoke_order.py`) | An unverified caller is denied and produces no output; denial is proved on an always-present privileged fixture, so it does not depend on any domain node's own logic |
| PB-5 | Node contract | Every domain node declares its required trust level explicitly; the backbone wiring is not overridden |
| PB-6 | State and persistence safety (`test_state_safety.py`) | Every persisted value is a plain type; the state survives a round trip; caller figures, the raw parameter mapping and any driver reference do not survive the run; no credential shape reaches the persisted state; a withheld report is not persisted |
| PB-7 | Human-review interrupt propagation (`test_pb7_hitl_interrupt_propagation.py`) | Not applicable — no node suspends the graph and the runtime config enables neither memory nor human review. Ships as a real module that skips with a stated reason and starts running if that changes. |

---

## 4. Integration tier — `tests/integration/test_invoke_endpoint.py`

Driven through the real ASGI application with bearer authentication.

| ID | Scope | Expected |
|---|---|---|
| IT-01 | Health | Reports the agent |
| IT-02 | Authentication — absent and wrong tokens | 401, with a body that does not say which |
| IT-03 | The sign-off payload | A released report |
| IT-04 | Two requests differing only in a KPI value | Different reports, each carrying its own figure — the check that separates a wired pipeline from one that emits a fixed baseline |
| IT-05 | Every audience, and each severity outcome | All reachable |
| IT-06 | Validation rejection — non-finite figures, absent parameters, unknown audience, instruction-shaped parameter | Error status and no output, rather than a generic answer |
| IT-07 | Credential screen — each recognised shape | 400 naming the field, never echoing the value |
| IT-08 | A credential inside a hostile field name | 400 identifying the field by position |
| IT-09 | Screen equivalence | Field-by-field screening refuses exactly when a whole-mapping scan finds something — the property that lets the refusal name a field without changing what is blocked |
| IT-10 | Oversized parameters | 413, before the graph runs |
| IT-11 | Envelope containment, domain violation | Error, blocked, the withheld notice, and the release slot present in the node history |
| IT-12 | Envelope containment, credential | The framework's scan fires one node earlier, so the release slot never runs; nothing credential-shaped reaches the caller |
| IT-13 | No traceback or source path in any refusal envelope | The framework turns an exception into an error update carrying a full traceback; none of it may surface |
| IT-14 | A stale successful value after a later failure | Not resurfaced |
| IT-15 | Clean-path control | The same request still produces its real report, so a refuse-everything gate cannot pass this suite |

---

## 5. Test-data notes

* KPI samples use the five default fields from `config/config.yaml`; band scenarios use
  the magnitudes in `config/kpi-thresholds.yaml`.
* Credential fixtures are synthetic and marked as examples. They are credential-*shaped*
  on purpose — the release checks and the adapter screen cannot be exercised otherwise.
* Individual-attribution fixtures use invented identifiers (`D-9001`,
  `driver John Smith`, `ドライバー山田太郎`) and never real data.
* The anonymisation salt is injected per test; no real salt is present in the repository.

---

## 6. Running locally

```bash
uv pip install --system -e ".[dev]"          # plus the framework wheel from the registry

pytest tests/ -v --tb=short
pytest tests/proof_of_boundary/ -v --tb=short
```
