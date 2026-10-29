# Phoenix → Lumoz Backfill

Copy traces you already have in [Arize Phoenix](https://github.com/Arize-ai/phoenix) into Lumoz.

The script reads spans from a Phoenix project, rebuilds them as the OpenTelemetry (OTLP) spans your application originally sent, and posts them to Lumoz. It remembers how far it got, so you can run it again later and it sends only what is new.

Use it to bring history into Lumoz when you start using Lumoz after Phoenix. To send **new** traces to both tools going forward, add a second exporter to your application instead — see the other examples in this repo.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill in your keys
```

| Variable | Required | Purpose |
|---|---|---|
| `LUMOZ_API_KEY` | yes | Your Lumoz API key (`client_id:client_secret`) |
| `PHOENIX_API_KEY` | no | Only if your Phoenix server requires authentication |
| `PHOENIX_COLLECTOR_ENDPOINT` | no | Phoenix URL. Default `http://localhost:6006` |
| `OTEL_ENDPOINT` | no | Lumoz ingest endpoint. Default `https://api.lumoz.ai/proxy/v1/traces` |

Values set in your shell override `.env`. Not sure your key and network work? Run [`connectivity/test_connectivity.py`](../../connectivity) first.

## Run

Run it with no arguments and it asks for what it needs:

```bash
python phoenix_to_lumoz.py
```

```
Phoenix projects:
  1. travel-planner
  2. default
Phoenix project (number or name): 1
Lumoz app [travel-planner]:
Lumoz env [phoenix]: prod
Framework that emitted these spans (name, or Enter to skip): langchain
Days of history to read [30]: 7
travel-planner -> app=travel-planner env=prod: reading 2026-10-17T... .. 2026-10-24T...
Send 128 spans from 'travel-planner' to Lumoz app=travel-planner env=prod? [Y/n]: y
  sent 128 spans (through 2026-10-24T17:57:24.022466+00:00)
Done: 128 spans sent.
```

Or pass everything as flags to run without prompts:

```bash
python phoenix_to_lumoz.py --project travel-planner --app travel-planner --env prod \
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
| `--ignore-watermark` | off | Re-read the whole `--days` window instead of resuming |
| `--dry-run` | off | Read and convert only; sends nothing and keeps the watermark |
| `-y`, `--yes` | off | Skip the confirmation prompt |
| `--phoenix-url` | `http://localhost:6006` | Phoenix server |
| `--endpoint` | see above | Lumoz ingest endpoint |
| `--state-file` | `~/.lumoz/phoenix_backfill_state.json` | Where progress is stored |

If you leave out `--project`, `--app` or `--env`, the script asks for those and for the framework, days and whether to resume. If you pass all three it runs without asking.

## Resuming

Progress is saved per **Phoenix project + app + env**, so each combination has its own position. After each batch is accepted by Lumoz the position moves forward; if a run is interrupted, the next one continues where it stopped. A run reads from the later of the saved position and `--days` ago.

Spans that reach Phoenix *after* the saved position has passed their start time are not picked up by a normal run. To catch those, re-read the window with `--ignore-watermark`. That can send spans a second time.

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

Run `python phoenix_to_lumoz.py --list-frameworks` for the full list. They are the [OpenInference](https://github.com/Arize-ai/openinference) instrumentation names, written with underscores (`llama_index`, `openai_agents`, `claude_agent_sdk`, …) plus a few others such as `google_adk`, `vercel_ai` and `mastra`. A typo gets a suggestion:

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
| `Not an interactive terminal` | Pass `--project`, `--app` and `--env` |
| `Nothing new to send` | Everything in the window was already sent. Use `--ignore-watermark` to re-read it |
| Cannot reach Phoenix | Check `PHOENIX_COLLECTOR_ENDPOINT` / `--phoenix-url`, and `PHOENIX_API_KEY` if auth is on |

## Notes

- The script imports `phoenix.trace`, which is internal to the Phoenix package, so `requirements.txt` pins `arize-phoenix`. This is the client library only; your Phoenix **server** can be any version.
- Your Phoenix data is only read, never changed.
