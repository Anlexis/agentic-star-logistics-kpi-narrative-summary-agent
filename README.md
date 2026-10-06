# Logistics KPI Narrative Summary Agent

AI agent for turning logistics KPI figures into narrative summaries for a chosen audience, built with Agentic Star.

> **Category**: Cat 2 (domain-specific pipeline)
> **Industry**: Logistics
> **Template ID**: LOG-C2-018

## Overview

Turns a period's logistics KPI figures into a written summary shaped for a particular
reader — a board, an operations team, or a customer.

Give it the current period's measures, the prior period's measures, and an audience. It
works out which measures moved, whether each movement is good or bad news *for that
particular measure*, how serious it is against configured bands, and returns a narrative
that says so — with the exceptions ranked and the actions ordered.

The judgement is deterministic. Whether a movement is an improvement depends on the KPI:
a falling cost per kilogram is good news, a falling on-time delivery rate is not. That
decision, and the severity banding behind it, are made in code from configuration before
any language model is involved. A model, when one is configured, phrases the conclusion —
it never reaches it. Without one, the report is composed directly from the same resolved
comparison, so the agent still produces a real report in a deployment that has no model
access.

Everything a caller can supply is bounds-checked on the way in: figures must be finite
and within range, field names are restricted to inert identifiers because they render
into the report, and driver references are anonymised or dropped rather than carried
through. On the way out, a report that names an individual or carries a credential shape
is withheld rather than trimmed.

Typical users are logistics operations and account teams who publish the same measures
every period to three different audiences.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >= 3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the framework version does not match, the agent fails
at graph compile / start-up preflight rather than starting in a partially working state. This
is intentional — a half-running agent is worse than one that refuses to start.

A missing *model* API key is a different matter and is handled, not fatal: the agent starts
and composes its reports deterministically from the same resolved comparison.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

To exercise the HTTP entry point locally:

```bash
export INVOKE_AUTH_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
uvicorn src.api.server:app --port 8000

curl -s -X POST http://localhost:8000/invoke \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d @deploy/invoke_payload.json
```

## Project Structure

```
src/          agent implementation (nodes, services, schemas, HTTP entry point)
tests/        unit, integration and boundary tests
config/       agent manifest, runtime settings, comparison bands, audience formats
prompts/      the model-facing narrative prompt
docs/         design and operational documentation
```

See `docs/` for the design and the test specification.

## Customising

Three of the four common changes need no code at all:

1. **Different KPIs** — edit `kpi_fields` in `config/config.yaml` and add a matching entry
   to `config/kpi-thresholds.yaml` with its direction and bands.
2. **Different sensitivity** — retune the `warning_delta` / `critical_delta` magnitudes.
   Leave `direction` alone unless the measure itself changed meaning.
3. **A different reader** — add a bundle to `config/audience-formats.yaml` with a token
   ceiling, an ordered section list and a tone.
4. **Different wording** — revise `prompts/narrative.md` for the model path, or the writer
   in `src/nodes/response_generate.py` for the deterministic one.

Re-run the test suite afterwards.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
