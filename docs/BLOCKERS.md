# Blockers, deferrals and assumptions

> Anything that needs a human decision, is stubbed, or was defaulted under BUILD.md's
> blocker protocol. Each entry states what was assumed, so the assumption can be overturned
> cheaply. "None" is a valid state for this file.

## Open — needs a human

_None._

## Defaulted (proceeding under a recorded assumption)

| # | Item | Default taken | Recorded in | Reversible by |
|---|---|---|---|---|
| D-1 | The plan left the live feed "TBD" | Public TDengine solar-fleet MQTT feed, `mqtt.tdengine.com:1883`, topic `inverters` | ADR-010 | Change `MQTT_HOST`/`MQTT_PORT`/`MQTT_TOPIC` in `.env` and add a `TopicMapping` in `vigil.ingest.solar_feed` |
| D-2 | The feed offers no history API, so the REST backfill in FR-1 cannot be built | Gap **detection** only; edge guarantee restated as at-most-once | ADR-011 | Only by switching to a feed that exposes history |
| D-3 | Inverters publish no expected-power field, unlike sites and strings | Use `PR_Local` as the normalised residual for that topic | ADR-012 | Swap the `primary=True` metric in the inverters `TopicMapping` |

## Known constraints on this machine (not blockers yet, will bite later)

| # | Constraint | Which phase it affects | Current plan |
|---|---|---|---|
| C-1 | GPU is an RTX 3050 Ti Laptop with 4 GB VRAM. A 7–8B tool-calling model on vLLM will not fit at fp16. | 4, 5 | Serve a quantized small model, or a hosted endpoint behind an env var, per BUILD.md section 5. Decide when Phase 4 starts; record as an ADR then. |
| C-2 | No API key of any kind is present in the environment. | 4 | The VLM path must work with a local model, or degrade cleanly and say so, rather than assuming a hosted endpoint. |
| C-3 | Docker VM has 8.1 GB of the machine's 15.6 GB. Kafka + Postgres + Flink + ClickHouse + MinIO together will be tight. | 2, 3 | Heaps are already pinned small in compose. Expect to run heavy services one phase at a time and to record what had to be stopped. |

## Resolved

| # | Item | Resolution |
|---|---|---|
| R-1 | Docker daemon was not running at session start | Docker Desktop lives under `%LOCALAPPDATA%\Programs\DockerDesktop`; started it. Stack comes up healthy with `docker compose up -d --wait`. |
