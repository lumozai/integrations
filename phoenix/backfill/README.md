# Phoenix → Lumoz Backfill

Copy traces you already have in [Arize Phoenix](https://github.com/Arize-ai/phoenix) into Lumoz.

The script reads spans from a Phoenix project, rebuilds them as the OpenTelemetry (OTLP) spans your application originally sent, and posts them to Lumoz. It remembers how far it got, so you can run it again later and it sends only what is new.

Use it to bring history into Lumoz when you start using Lumoz after Phoenix. To send **new** traces to both tools going forward, add a second exporter to your application instead — see the other examples in this repo.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env    # then fill in your keys
```

| Variable | Required | Purpose |
|---|---|---|
| `LUMOZ_API_KEY` | yes | Your Lumoz API key (`client_id:client_secret`) |
| `PHOENIX_API_KEY` | no | Only if your Phoenix server requires authentication |
| `PHOENIX_COLLECTOR_ENDPOINT` | no | Phoenix URL. Default `http://localhost:6006` |
| `OTEL_ENDPOINT` | no | Lumoz ingest endpoint. Default `https://api.lumoz.ai/proxy/v1/traces` |
| `REDACT_SALT` | no | Salt for `hash` redaction (see [Redacting PII](#redacting-pii)) |

Values set in your shell override `.env`. Not sure your key and network work? Run [`connectivity/test_connectivity.py`](../../connectivity) first.

## Run

Run it with no arguments and it asks for what it needs:

```bash
uv run phoenix_to_lumoz.py
```

```
Connecting to Phoenix at http://localhost:6006 ...
  connected: 2 project(s) found
Loading the Phoenix client library (slow on the first run) ...
Phoenix projects:
  1. travel-planner
  2. default
Phoenix project (number or name): 1
Lumoz app [travel-planner]:
Lumoz env [phoenix]: prod
Framework that emitted these spans (name, or Enter to skip): langchain
Days of history to read [30]: 7
travel-planner -> app=travel-planner env=prod: reading 2026-10-17T... .. 2026-10-24T...
Counting what is new (nothing is sent yet) ...
  128 spans read from Phoenix
Send 128 spans from 'travel-planner' to Lumoz app=travel-planner env=prod? [Y/n]: y
Reading from Phoenix and sending in batches to https://api.lumoz.ai/proxy/v1/traces ...
  sent 128 of 128 spans (through 2026-10-24T17:57:24.022466+00:00)
Done: 128 spans sent.
```

Or pass everything as flags to run without prompts:

```bash
uv run phoenix_to_lumoz.py --project travel-planner --app travel-planner --env prod \
    --framework langchain --days 7 --yes
```

Try `--dry-run` first to read and convert without sending anything.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--project` | prompted | Phoenix project to read |
| `--app` | the Phoenix project name | Lumoz app name (sent as `service.name`) |
| `--env` | `phoenix` | Lumoz environment (sent as `deployment.environment`) |
| `--days` | `30` | How far back to read |
| `--framework` | none | Library that produced the spans, e.g. `langchain`, `openai`, `google_adk`. See `--list-frameworks` |
| `--allow-unknown-framework` | off | Accept a `--framework` that is not in the list |
| `--redact` | off | Redact PII before sending. See [Redacting PII](#redacting-pii) |
| `--redact-config` | none | YAML with custom patterns and per-attribute rules; implies `--redact` |
| `--show-redactions` | off | Print what was redacted, with samples of the **original** text |
| `--ignore-watermark` | off | Re-read the whole `--days` window instead of resuming |
| `--dry-run` | off | Read and convert only; sends nothing and keeps the watermark |
| `-y`, `--yes` | off | Skip the confirmation prompt |
| `--phoenix-url` | `http://localhost:6006` | Phoenix server |
| `--endpoint` | see above | Lumoz ingest endpoint |
| `--state-file` | `~/.lumoz/phoenix_backfill_state.json` | Where progress is stored |

If you leave out `--project`, `--app` or `--env`, the script asks for those and for the framework, whether to redact PII (default no), days and whether to resume. If you pass all three it runs without asking.

## Resuming

Progress is saved per **Phoenix project + app + env**, so each combination has its own position. After each batch is accepted by Lumoz the position moves forward; if a run is interrupted (Ctrl-C prints what it was doing and how much was saved), the next one continues where it stopped. A run reads from the later of the saved position and `--days` ago.

Spans that reach Phoenix *after* the saved position has passed their start time are not picked up by a normal run. To catch those, re-read the window with `--ignore-watermark`. That can send spans a second time.

## Redacting PII

Redaction is **off by default**. Add `--redact` to remove personal data from spans on your machine, before they are sent:

```bash
uv run phoenix_to_lumoz.py --redact --dry-run --show-redactions   # preview first
uv run phoenix_to_lumoz.py --redact                               # then send
```

```
Redacted 10 item(s) in 2 span(s):
       1  credit_card  input.value
       1  email        llm.input_messages.*.message.content
       1  phone        metadata.note
       ...
Samples (these show the ORIGINAL text; do not share this output):
  email        llm.input_messages.*.message.content: 'jane@example.com' -> [REDACTED:EMAIL]
```

**What it finds** (pattern matching, with checks to avoid obvious false positives):

| Detector | Matches |
|---|---|
| `email` | Email addresses |
| `phone` | Numbers with a `+` country code, or with separators or parentheses, such as `(415) 555-0132` or `415-555-0132`. A bare run of digits is not treated as a phone number |
| `ssn` | US Social Security numbers (`123-45-6789`) with invalid ranges rejected |
| `credit_card` | 13–19 digit card numbers that pass the Luhn check |
| `ip` | IPv4 and IPv6 addresses |
| `secret` | Common API-key shapes (`sk-…`, `AKIA…`, `ghp_…`, `xox…-`, Google `AIza…`), JWTs and `Bearer` tokens |

It scans every text value: inputs and outputs, message contents, tool parameters, `metadata`, document text, events such as exception messages, the span name and the status message. Matches become `[REDACTED:EMAIL]` and so on. Numbers, IDs, timestamps and attribute names are not changed.

**Left as-is by default:** `user.id`, `session.id`, `openinference.span.kind`, MIME types, `llm.model_name`, `llm.provider`, `llm.system`, `llm.finish_reason`, `tool.name` and token counts. Add a rule in the config to change any of them.

### Config file

`--redact-config redact.yaml` lets you add your own patterns, turn detectors off, and set rules for specific attributes. See [`redact.example.yaml`](redact.example.yaml):

```yaml
patterns:
  - name: employee_id
    regex: 'EMP-\d{6}'
attributes:
  metadata.customer_email: drop     # drop | mask | hash | keep
  metadata.account_number: hash
action: mask                        # mask or hash for detector matches
```

- `hash` replaces a value with a stable code such as `[EMAIL:3f9a1c0b2e]`, so the same person still groups together. Without a salt, hashes of guessable values (emails, phone numbers) can be reversed by trying candidates, so set `salt:` in the config or the `REDACT_SALT` variable.
- Attribute rules use `*` wildcards on the attribute name and override the detectors and the default list above.

### Things to know

- **Best effort, not a guarantee.** It does not find names, street addresses or other free-text identifiers. Run with `--dry-run --show-redactions` on your own data and review the output before sending.
- **If redaction fails, nothing more is sent.** The script stops rather than send a span unredacted.
- **Past runs are not re-redacted.** Spans already sent stay as they were sent. The script records the redaction settings with the saved position and warns when they change between runs.
- **Redacted text in JSON.** A pattern can match inside a JSON string and change its value, but the structure stays valid.
- `--show-redactions` prints the original text. Keep that output private.

Tests: `uv run python -m unittest test_redact -v` (no network or Phoenix needed).

## What is copied

What Phoenix keeps is rebuilt as it was sent: span and trace IDs, parent links, names, start and end times, status, events, and all attributes, including the OpenInference span kind, messages, token counts, and JSON-valued attributes such as `metadata`.

Phoenix does not keep everything it receives, so a few things cannot be restored:

| Not stored by Phoenix | What the script does |
|---|---|
| Resource attributes (`service.name`, `deployment.environment`, …) | Sets `service.name` and `deployment.environment` from `--app` and `--env`, and `project.name` from the Phoenix project |
| Which library emitted the span | Set it with `--framework`; otherwise it is left blank |
| Span links, trace state, flags, OTel span kind | Left out |

Also from Phoenix's own storage: timestamps have microsecond precision, and the key order inside JSON-valued attributes may differ from the original.

Every backfilled span carries the resource attribute `backfill.source=phoenix`.

### `--framework` names

Run `uv run phoenix_to_lumoz.py --list-frameworks` for the full list. They are the [OpenInference](https://github.com/Arize-ai/openinference) instrumentation names, written with underscores (`llama_index`, `openai_agents`, `claude_agent_sdk`, …) plus a few others such as `google_adk`, `vercel_ai` and `mastra`. A typo gets a suggestion:

```
Unknown framework 'langchian'. Did you mean: langchain, langchain_v0?
```

LangGraph applications are traced by the LangChain instrumentation, so use `langchain`.

## Large projects and big requests

- Spans are sent in batches of up to 500, and each request is kept under a size limit. If Lumoz still answers `413 Request Too Long`, the script splits the batch in half and retries, as many times as needed.
- A single span too large for Lumoz to accept stops the run with a message naming it. The saved position does not move past it.
- Phoenix servers older than the version that added sorting return spans newest-first. The script detects this and reads the whole window into memory to sort it. For a very large window, use a smaller `--days`.

## Troubleshooting

| Symptom | Check |
|---|---|
| `Set LUMOZ_API_KEY` | Put the key in `.env` or export it |
| `403` mentioning `Credential` / `Signature` | The request reached the wrong endpoint. Use the default endpoint or the one Lumoz gave you |
| `401` / `403` otherwise | Key format is `client_id:client_secret`; try `connectivity/test_connectivity.py` |
| `Input ended before every question was answered` | Running without a terminal: pass `--project`, `--app` and `--env` |
| `Nothing new to send` | Everything in the window was already sent. Use `--ignore-watermark` to re-read it |
| Stops at `Connecting to Phoenix` or `Cannot reach …` | The connection attempt gives up after 10 seconds. Check the URL and that the server is running. |
| Cannot reach Phoenix | Check `PHOENIX_COLLECTOR_ENDPOINT` / `--phoenix-url`, and `PHOENIX_API_KEY` if auth is on |

## Notes

- The script imports `phoenix.trace`, which is internal to the Phoenix package, so `pyproject.toml` pins `arize-phoenix`. This is the client library only; your Phoenix **server** can be any version.
- Your Phoenix data is only read, never changed.
- Redaction applies to what is sent to Lumoz. It never modifies Phoenix.
