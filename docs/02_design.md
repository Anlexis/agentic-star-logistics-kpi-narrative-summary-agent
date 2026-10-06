# Logistics KPI Narrative Summary Agent — Design

**Template ID:** LOG-C2-018
**Name:** LogisticsKPINarrativeSummaryAgent

---

## 1. Overview

The agent turns a period's logistics KPI figures into a written summary shaped for a
particular reader. It takes the current period's values, the prior period's values for
comparison, and an audience, and returns a narrative that says which measures moved,
which movements matter, and what to do first.

The work that makes the report trustworthy is deterministic and happens before any
model is involved: whether a movement is an improvement or a deterioration depends on
the KPI, and severity is decided by comparing the movement against configured bands.
A language model, when one is configured, phrases that conclusion. It never reaches it.

| Field | Value |
|---|---|
| Category | Cat 2 |
| Industry | LOG (Logistics) |
| Pattern | Structured input in, narrative out. No retrieval. |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Generation mode | `llm` — a model is called whenever one is configured |

### 1.1 What problem it solves

Logistics KPI reporting is a recurring, high-volume writing task: the same measures,
every period, re-explained for a board, an operations team and a customer in three
different registers. Drafting each by hand takes hours and the interpretation drifts
between periods and between authors. The interpretation rules — which direction is
good for which KPI, and how large a movement has to be before it is worth raising —
are stable and belong in configuration, not in a writer's head.

---

## 2. Architecture

### 2.1 Backbone and slots

The framework owns the graph wiring. This template fills the three domain slots and
does not override the edges.

```
START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END

  pre_process    InputValidate -> IntentClassify
  main           ResponseGenerate
  post_process   OutputValidate
```

Each slot runs its ordered sub-nodes in turn, merging their partial updates and
stopping at the first refusal so an error status reaches the router intact. A slot
handed a state that already carries an error passes it through untouched: the backbone
visits every slot, so a later slot must not overwrite an earlier refusal.

### 2.2 Nodes

| Node | Responsibility | Reads from runtime config |
|---|---|---|
| `InputValidateNode` | Owns the caller-data contract. Screens the structured parameters for instruction-shaped content, checks every field against explicit bounds, anonymises or drops driver references, writes the validated values into their own state fields, and clears the raw parameter mapping. | `kpi_fields` |
| `IntentClassifyNode` | Resolves `audience_type` to a format bundle — target length, required sections, tone. | `audience_formats_path` |
| `ResponseGenerateNode` | Computes the per-KPI comparison against the baseline, then produces the narrative: through a configured model, or through the deterministic writer when none is configured. | `kpi_thresholds_path`, `narrative_prompt_path`, `llm` |
| `OutputValidateNode` | The release boundary. Runs the release checks, withholds the report when any of them fails, and appends the advisory disclaimer when the generated text omits it. | `output_disclaimer` |

### 2.3 State

State is a flat TypedDict extending the framework's `AgentState`, declared in
`src/schemas/state.py` and returned by the graph's `state_schema`. That last part is
load-bearing: the compiled graph derives its channels from the declared schema, so a
field the schema does not declare is dropped between nodes.

| Field | Written by | Notes |
|---|---|---|
| `kpi_data`, `baseline_data` | `InputValidateNode` | Validated figures as JSON strings. Cleared before the run ends. |
| `audience_type` | `InputValidateNode` | One of `executive`, `operations`, `customer`. |
| `format_params` | `IntentClassifyNode` | The resolved format bundle, as a JSON string. |
| `delta_summary` | `ResponseGenerateNode` | Per-KPI direction, delta and severity, as a JSON string. |
| `narrative_output`, `formatted_output` | `ResponseGenerateNode`, `OutputValidateNode` | The report. Cleared on a refusal. |
| `blocked` | `OutputValidateNode` | True when the release checks withheld the report. |

Only primitives and JSON strings are stored: checkpoints are serialised with msgpack,
so an object in state is an object in storage. Nothing credential-shaped and no
identity is written to state — driver references are anonymised or dropped on the way
in, and the caller's raw parameters are cleared once the validated values are extracted.

### 2.4 Entry points

`src/api/server.py` is a standalone HTTP adapter: authentication, a size ceiling, and a
credential screen over the structured parameters. It holds no domain logic, so a caller
reaching the agent through the hosted platform instead gets the same behaviour.

---

## 3. Configuration

Two files, with different jobs.

`config/agent.yaml` is the static manifest: identity, entry-point class, required trust
level, and the compile-time requirements. Runtime parameters do not belong here — the
registry reads this file for discovery only.

`config/config.yaml` carries the runtime parameters and is passed to the graph
constructor. The standalone adapter loads the same file the same way, so a declared
value is live in both deployments rather than only one.

```yaml
max_retry: 3
timeout_s: 30

llm:                       # applied only when an API key is provisioned
  model: gpt-4o
  max_tokens: 1024
  temperature: 0.2

kpi_fields:                # operators override this list per deployment
  - otd_pct
  - exception_rate
  - cost_per_kg
  - warehouse_utilization_pct
  - damage_rate

kpi_thresholds_path: config/kpi-thresholds.yaml
audience_formats_path: config/audience-formats.yaml
narrative_prompt_path: prompts/narrative.md

output_disclaimer: "for internal reporting purposes"

security:
  s3_gate_enabled: true    # mandatory; false raises at construction
```

### 3.1 Comparison bands (`config/kpi-thresholds.yaml`)

Each KPI declares which direction is good and how far it has to move the wrong way
before the movement is a warning or a critical exception. This is the file that keeps
a falling unit cost from being reported as a problem and a falling on-time rate from
being reported as an improvement.

```yaml
otd_pct:
  direction: higher_is_better
  warning_delta: -0.05
  critical_delta: -0.10
```

The two band values are configuration, but they are numeric input all the same: a
non-finite band makes every severity comparison return false and reports a real breach
as normal, so both are checked when the node is constructed.

### 3.2 Audience formats (`config/audience-formats.yaml`)

Three bundles — `executive`, `operations`, `customer` — each declaring a token ceiling,
an ordered section list and a tone descriptor. Adding an audience is a configuration
change; the section names it uses map onto the writer's content kinds, and an
unrecognised name falls back to the per-KPI movement list.

---

## 4. The caller-data contract

Everything the caller controls arrives in the structured invocation parameters and is
checked in one place before any of it is used.

| Parameter | Required | Rule |
|---|---|---|
| `kpi_data` | yes | An object of at most 64 entries. Every key is a lowercase identifier of at most 32 characters; every value is a finite number within ±1e9. |
| `baseline_data` | no | Same shape and same rules. Absent means every comparison is reported as unknown rather than assumed to be zero. |
| `audience_type` | no | One of the configured audiences. Defaults to `executive`. |

Three rules cover the surface:

**Numbers are finite and bounded.** `NaN` and the infinities parse as floats and then
compare false against every threshold, so an unchecked value would be classified as
normal rather than rejected — on exactly the decision this agent exists to make. Every
caller-supplied number goes through a finite, bounded check that also rejects booleans
and non-numerics.

**Strings that reach the report are inert.** A KPI field name renders into the
narrative, so it is restricted to a short lowercase identifier; a driver reference is
restricted to a short alphanumeric token. Free text in either position would be
caller-controlled output.

**Instruction-shaped content is refused first.** The parameters are screened
depth-first, keys included, before any structural check runs — so a hostile field name
is refused as an injection attempt rather than as an unknown key. The screen covers
chat-template control tokens as a class (`<|…|>`, `[INST]`, `<<SYS>>`) as well as
directive phrases, and it screens both the raw text and a markup-stripped form: the
strip erases control tokens, and re-assembles directives that were spliced with inline
tags, so screening only one of the two forms misses one of the two attacks.

Refusals name the field, never the value.

### 4.1 Driver references

Aggregate KPIs do not identify people, but exception and damage analyses sometimes
carry a driver reference in the source data. Any field whose name contains
`driver_ref` is replaced by a salted digest, or dropped entirely when no salt is
provisioned. The salt is a deployment secret read with `get()` rather than `require()`:
a deployment without one is a valid deployment — it drops references instead of hashing
them — and requiring the key would stop the agent loading at all. A salt shorter than
16 characters is treated as absent, because a short salt is brute-forceable against a
small driver population.

---

## 5. Security

| Layer | Where | Behaviour |
|---|---|---|
| Trust | Framework, on every node | Every domain node declares `VERIFIED_EXTERNAL`. The standalone adapter is the boundary that establishes it; an unauthenticated caller is denied before any node body runs. |
| Input | `InputValidateNode` + framework | The framework masks and screens the free-text request field. The node owns the structured parameters: instruction screen, inert identifiers, finite bounds, entry caps. Enforced in the node rather than relying on the framework alone, so the guarantee holds where the framework gate is configured differently. |
| Output | `OutputValidateNode` + framework | The release checks below, plus the framework's own scan over every value the node returns. |
| Audit | Every node | Each node emits a domain event on its execution path, in addition to the framework's own lifecycle events. |
| Secrets | Adapter and services | Read through the secret provider, never from the process environment inside the graph. Nothing credential-shaped is written to state. |

### 5.1 Release checks

`OutputValidateNode` refuses to release a report that fails any of these:

| Rule | Behaviour on failure |
|---|---|
| No credential-shaped value in the report | Withheld. The scan is the framework's own credential detector, not a local pattern list. |
| No individual identified — no operational driver reference, no personal name attributed to a driving role, no anonymised digest | Withheld. |
| The advisory disclaimer is present | Appended, not refused: a missing disclaimer is a formatting slip. |

The credential scan reuses the framework detector deliberately. A local list narrower
than the framework's is not a shortcut but a bypass: the framework re-scans every value
the node returns, raises on one the node missed, and the framework wrapper then
discards the node's whole update — including the clearing that the refusal depends on.

### 5.2 A refusal withholds

On any violation the node returns an error status **and** overwrites every state field
that carries report text or a payload, leaving a non-empty notice in its place. The
framework's own output accessor resolves the response as the first non-empty of two
result fields with no status check, so an error status alone does not withhold
anything — the ungated report ships inside the error envelope. An empty-string
placeholder does not help either: it is falsy, so it re-activates the same fallback.

The graph's `get_output` closes the same hole from the other side: on anything other
than success it returns the node's own refusal notice or nothing, never a value left in
state by an earlier step.

Both are needed, and each is provable on its own. Measured against the real framework:

* Removing the field clearing leaves the graph accessor containing the response but the
  refused report still present in the persisted state.
* A credential-shaped report never reaches the release checks at all — the framework's
  scan fires one node earlier, on the node that produced the value, and the wrapper
  returns a bare error update that clears nothing. On that path the graph accessor is
  the only thing between the caller and the ungated state.

### 5.3 Credential-shaped parameters

A credential-shaped string anywhere in the structured parameters fails the *first* node
of the graph, before any of this template's code runs: that node returns the parameters
verbatim in its own result and the framework scans every value of every result. The
caller receives an error naming nothing, and on a hosted conversation the same
parameters replay every turn, so the session does not recover.

The request cannot succeed either way, so the adapter screens for it and refuses with
a message naming the offending field. It calls the same detector the framework gate
calls, on the same object, so what the adapter refuses and what the gate blocks are one
set by construction. Field names are caller data too, so a name is echoed only when it
is short, inert and not itself credential-shaped; otherwise the field is identified by
position.

---

## 6. Test surface

| Tier | Location | Covers |
|---|---|---|
| Unit | `tests/unit/` | The caller-data contract including a non-finite matrix per field, the comparison arithmetic, both narrative paths, the release checks and the refusal's clearing behaviour, and the state contract. |
| Boundary | `tests/proof_of_boundary/` | Import isolation, backbone order through a full invoke, the trust-gate denial path, state and persistence safety. |
| Integration | `tests/integration/` | The real HTTP entry point: authentication, real output computed from caller figures, every audience and severity path, validation rejection, the credential screen, the size ceiling, and envelope containment on every refusal path. |

Full detail in `docs/03_test_spec.md`.

---

## 7. Dependencies

* The AgentCore framework wheel, installed by CI. Not declared in `pyproject.toml`
  because it is not resolvable from the default package index.
* `pytest`, `pytest-cov`, `pytest-asyncio`, `ruff`, `mypy`, `types-PyYAML` for
  development, all exactly pinned.
* No knowledge base and no vector store: this template does not retrieve.
