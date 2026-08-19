# GX Market Info integration

This extension keeps the upstream `TradingAgentsGraph.propagate()` and
`tradingagents` CLI unchanged. It adds a durable stage runner and the
`tradingagents-gx` command for data from `g_market_info_1229`.

Sơ đồ so sánh các phần giữ nguyên, mở rộng và bổ sung so với repo upstream nằm
tại [tradingagents-apg-architecture.mmd](tradingagents-apg-architecture.mmd).

## Install

Use Python 3.12 even though the package permits Python 3.10+. From the repository
root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,gx-postgres]"
```

Nếu bật archive FireAnt, RSS và macro Việt Nam, cài thêm các optional extra:

```bash
python -m pip install -e ".[dev,gx-postgres,fireant,vn-media,vn-macro]"
```

`vn-macro` bổ sung parser SDMX/Excel cho dữ liệu công khai NSO/SBV và không cần
encryption key.

The recommended data path is the GX HTTP API. Direct read-only PostgreSQL access
is optional, but it is currently required when the fundamental analyst needs GX
cash-flow data. If PostgreSQL will never be used, omit `gx-postgres`:

```bash
python -m pip install -e ".[dev]"
```

Never commit a populated `.env` file. The repository ignores `.env.*` except
example templates.

## Hosted LLM

Copy the template and add a project-scoped key locally:

```bash
cp .env.hosted.example .env.hosted
tradingagents-gx --env-file .env.hosted doctor
tradingagents-gx --env-file .env.hosted full --ticker HPG --date 2026-08-12
```

`OPENAI_API_KEY` is read only from the environment. Hosted LLM is the recommended
development and production baseline because tool calling and structured output
are central to the analyst pipeline.

The same runner supports every provider already implemented by TradingAgents.
Quick and Deep are independent profiles: set
`TRADINGAGENTS_QUICK_LLM_PROVIDER` / `TRADINGAGENTS_DEEP_LLM_PROVIDER`, their
matching `*_THINK_LLM` model IDs and separate `*_LLM_BASE_URL` values. Optional
role keys override, then fall back to the matching provider credential:

| Provider value | Credential/configuration |
| --- | --- |
| `openai` | `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `google` | `GOOGLE_API_KEY` |
| `xai`, `deepseek`, `openrouter` | `XAI_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY` |
| `qwen`, `qwen-cn` | `DASHSCOPE_API_KEY`, `DASHSCOPE_CN_API_KEY` |
| `glm`, `glm-cn` | `ZHIPU_API_KEY`, `ZHIPU_CN_API_KEY` |
| `minimax`, `minimax-cn` | `MINIMAX_API_KEY`, `MINIMAX_CN_API_KEY` |
| `mistral`, `kimi`, `groq`, `nvidia` | `MISTRAL_API_KEY`, `MOONSHOT_API_KEY`, `GROQ_API_KEY`, `NVIDIA_API_KEY` |
| `azure` | role base URL, role model as deployment, `AZURE_OPENAI_API_KEY` (or role key), `OPENAI_API_VERSION` |
| `bedrock` | Install `.[bedrock]`; use `AWS_BEARER_TOKEN_BEDROCK` or the AWS credential chain, plus an AWS region |

Both profiles belong in the same env file. For example, Quick can use local
Ollama while Deep uses hosted OpenAI:

```dotenv
TRADINGAGENTS_QUICK_LLM_PROVIDER=ollama
TRADINGAGENTS_QUICK_THINK_LLM=qwen3:8b
TRADINGAGENTS_QUICK_LLM_BASE_URL=http://127.0.0.1:11434/v1

TRADINGAGENTS_DEEP_LLM_PROVIDER=openai
TRADINGAGENTS_DEEP_THINK_LLM=gpt-5.5
TRADINGAGENTS_DEEP_LLM_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=...
```

`TRADINGAGENTS_LLM_PROVIDER` and `TRADINGAGENTS_LLM_BACKEND_URL` remain legacy
fallbacks when a role value is absent. The explicit `--env-file` wins over stale
shell values. `doctor` reports Quick and Deep separately and never invokes a paid
hosted model.

## Ollama on Apple Silicon

On macOS, run Ollama natively so it can use Metal. With 16 GB RAM, start with one
8B tool-capable model for both quick and deep roles:

```bash
ollama serve
ollama pull qwen3:8b
cp .env.ollama.example .env.ollama
tradingagents-gx --env-file .env.ollama doctor
tradingagents-gx --env-file .env.ollama full --ticker HPG --date 2026-08-12
```

Docker Desktop on macOS does not pass the Apple GPU through to an Ollama
container. If TradingAgents itself runs in Docker, keep Ollama native and set
`OLLAMA_BASE_URL=http://host.docker.internal:11434/v1`.

Lưu ý riêng với FireAnt: chỉ endpoint loopback (`localhost`, `127.0.0.0/8`,
`::1`) được phân loại là local. `host.docker.internal`, IP LAN và Ollama từ xa
được xử lý như hosted, nên phải có phê duyệt và bật
`TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED=true` trước khi gửi social content.

For vLLM, LM Studio or llama.cpp, use `openai_compatible`, the server's exact
model ID and its `/v1` base URL:

```dotenv
TRADINGAGENTS_QUICK_LLM_PROVIDER=openai_compatible
TRADINGAGENTS_QUICK_LLM_BASE_URL=http://127.0.0.1:1234/v1
TRADINGAGENTS_QUICK_THINK_LLM=exact-server-model-id
TRADINGAGENTS_DEEP_LLM_PROVIDER=openai_compatible
TRADINGAGENTS_DEEP_LLM_BASE_URL=http://127.0.0.1:1234/v1
TRADINGAGENTS_DEEP_THINK_LLM=exact-server-model-id
#TRADINGAGENTS_QUICK_LLM_API_KEY=
#TRADINGAGENTS_DEEP_LLM_API_KEY=
```

The served model must support Chat Completions, function/tool calls and structured
output. A text-only model cannot reliably run the market/news/fundamental agents.

## Docker

The Python virtual environment is the simplest development path. Docker is useful
for reproducible deployment. The upstream image defaults to the interactive
`tradingagents` entrypoint, so select the GX entrypoint explicitly:

```bash
cp .env.hosted .env
# When GX runs on the macOS host, set this in .env:
# GX_MARKET_INFO_BASE_URL=http://host.docker.internal:5005
docker compose build tradingagents
docker compose run --rm --entrypoint tradingagents-gx tradingagents doctor
docker compose run --rm --entrypoint tradingagents-gx tradingagents \
  full --ticker HPG --date 2026-08-12
```

The existing `tradingagents_data` volume preserves session JSON and memory across
containers. For a fully containerized Ollama flow:

```bash
docker compose --profile ollama up -d ollama
docker compose exec ollama ollama pull qwen3:8b
cp .env.ollama.example .env
docker compose --profile ollama run --rm --entrypoint tradingagents-gx \
  tradingagents-ollama doctor
```

On Apple Silicon, native Ollama remains the recommended local-development option
because Docker Desktop does not expose Metal acceleration to the container.

## Run individual stages

Create a new run by executing one analyst:

```bash
tradingagents-gx --env-file .env.hosted stage market \
  --ticker HPG --analysis-date 2026-08-12
```

A new `full` or `stage` run requires exactly one time mode. Use `--date` for the
documented 15:00 Vietnam close cutoff, or `--as-of-now` to freeze one immutable,
timezone-aware current cutoff:

```bash
tradingagents-gx --env-file .env.hosted full \
  --ticker VIC --as-of-now
```

For a one-shot live refresh, `--collect-evidence` runs the Vietnam media
collector first and the FireAnt collector second for the explicit ticker. Each
lane is isolated: a sanitized warning from one lane does not prevent the other
lane or the run. The clock is read exactly once after both attempts and that
cutoff is persisted by the new session:

```bash
tradingagents-gx --env-file .env.hosted stage news \
  --ticker VIC --as-of-now --collect-evidence --analysts news
```

This bounded preflight does not collect NSO/SBV macro data and does not invoke
an LLM. Macro remains an independently scheduled/archive-backed lane. Without
`--collect-evidence`, the live clock is frozen immediately before session
creation.

The command prints a session path with this stable layout:

```text
~/.tradingagents/runs/HPG/2026-08-12/<run_id>/session.json
```

Resume later using that exact path:

```bash
tradingagents-gx --env-file .env.hosted stage sentiment --session <session.json>
tradingagents-gx --env-file .env.hosted stage news --session <session.json>
tradingagents-gx --env-file .env.hosted stage fundamentals --session <session.json>
tradingagents-gx --env-file .env.hosted stage research --session <session.json>
tradingagents-gx --env-file .env.hosted stage trader --session <session.json>
tradingagents-gx --env-file .env.hosted stage risk --session <session.json>
tradingagents-gx show <session.json>
```

Resume commands accept the session path and stage only. Do not add `--date`,
`--as-of-now`, or `--collect-evidence`: the stored time mode and cutoff are part
of the immutable run identity. `--collect-evidence` is valid only for a new
`--as-of-now` run, never for close-date mode or resume.

`research` requires at least one completed, non-empty selected analyst report;
`research` is required by `trader`; `trader` is required by `risk`. Re-running an analyst invalidates
research, trader and risk outputs. Re-running research invalidates trader and
risk. Reports that are unavailable, failed, or not run are represented by explicit
markers in downstream prompts rather than silently empty text. The ticker, date,
analyst selection, LLM provider/models/endpoint/output profile and GX transport
are immutable; changing any of them requires a new run.

Session JSON uses `schema_version: 6` and is replaced atomically. V1-V5 files
migrate to an immutable close-mode cutoff at 15:00 `Asia/Ho_Chi_Minh`; new live
runs persist their exact timezone-aware cutoff. Earlier schemas also migrate to
immutable Quick/Deep identities; a former shared provider/base URL is assigned
to both roles. V1/V2 files receive the appropriate legacy social/media profiles;
every pre-v5 file receives `macro_profile=legacy`.
A FireAnt run has an immutable,
non-secret `social_profile` (provider, lookback, thresholds, archive ID and prompt
version), so changing that profile requires a new run. Sentiment/News also bind
an immutable, non-secret `media_profile` (providers, lookback, threshold, archive
ID, schema, alias policy and prompt version). Changing either role's provider,
model or base URL requires a new run. Override the session root with
`TRADINGAGENTS_STAGE_RUNS_DIR`.

GX News additionally binds an immutable `macro_profile` containing the NSO/SBV
provider set, lookback, indicator/prompt versions, archive UUID/schema and strict
PIT policy. A legacy session cannot silently resume under this new profile.

Stage metadata records each analyst tool's configured vendor chain. Where the
Sentiment Analyst returns observed source metadata, the session additionally
stores the actual provider, availability, sample size, unique-author count,
window, fetch/snapshot ID, point-in-time quality and warnings. Raw posts and
author identity are excluded from `session.json`. Legacy tools still label their
vendor information as configured intent when actual provenance is unavailable.
GX result objects carry their own transport and point-in-time metadata inside the
adapter.

The same operations are available from Python:

```python
from tradingagents.default_config import apply_gx_market_info_defaults
from tradingagents.graph.stage_runner import TradingAgentsStageRunner

runner = TradingAgentsStageRunner(apply_gx_market_info_defaults())
session = runner.create_session(
    "HPG", "2026-08-12", selected_analysts=("market", "fundamentals")
)
path = session.save()
runner.run_stage_to(session, "market", session_path=str(path))
runner.run_stage_to(session, "research", session_path=str(path))
```

Programmatic live runs must supply an aware, frozen cutoff explicitly; the
runner never reads the clock on their behalf:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

cutoff = datetime(2026, 8, 19, 16, 5, 31, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
session = runner.create_session(
    "CTG",
    "2026-08-19",
    analysis_mode="live",
    analysis_cutoff=cutoff,
)
```

The runner loads only resolved memory entries dated before `analysis_date`, so a
historical run cannot consume later decisions or reflections.

## GX configuration and troubleshooting

### Start the GX data path

The HTTP transport expects the existing GX runtime, not an embedded database.
Start `gx.cache` and `g.info` with their normal database/RPC configuration, then
start `gx.api`. The database connection used by those services must target
`g_market_info_1229`; this integration adds no migration and does not create or
seed that database.

Outside development/test, `gx.api` now refuses to boot unless both server-side
credentials are non-empty:

```dotenv
GX_API_TV_API_KEY=<server-tv-token>
GX_ANALYSIS_DATA_API_KEY=<server-analysis-token>
```

On the Python side, set `GX_MARKET_INFO_TV_TOKEN` to the same value as the
server's `GX_API_TV_API_KEY`, and set `GX_ANALYSIS_DATA_API_KEY` to the same
analysis token. Keep both in an ignored environment file; never pass either in a
query string. Because GX services also depend on the existing node names,
cookie, RabbitMQ and database settings, use their deployment environment or
project runbook rather than starting only `gx.api` in isolation.

After all three services are healthy, verify the complete path without spending
LLM tokens:

```bash
tradingagents-gx --env-file .env.hosted doctor
```

`doctor` must report the last completed trading session, HPG candles with VND
units, and the configured LLM/model before a full run.

API transport is the default:

```dotenv
GX_DATA_TRANSPORT=api
GX_MARKET_INFO_BASE_URL=http://127.0.0.1:5005
GX_MARKET_INFO_API_VERSION=v1.0.7
GX_MARKET_INFO_TV_TOKEN=
GX_ANALYSIS_DATA_API_KEY=
GX_DATA_TIMEOUT_SECONDS=10
GX_MARKET_INFO_EXPECTED_DB=g_market_info_1229
```

For controlled internal deployments that can reach PostgreSQL directly, install
the optional driver and put these values into the same ignored hosted/Ollama
profile (the standalone example is `.env.gx-postgres.example`):

```dotenv
GX_DATA_TRANSPORT=postgres
GX_MARKET_INFO_DATABASE_URL=postgresql://readonly_user:password@db-host:5432/g_market_info_1229
GX_MARKET_INFO_EXPECTED_DB=g_market_info_1229
GX_DATA_TIMEOUT_SECONDS=10
```

### Cash-flow transport behavior

With `GX_DATA_TRANSPORT=postgres`, the GX adapter reads cash-flow statements
directly from `public.fiin_cashflow`, using the same read-only connection and
statement timeout as the other PostgreSQL fundamentals queries. Historical
queries apply strict point-in-time predicates: the row must be active, its
`publicdate` and `createdate` must be present and no later than `as_of`, and its
`updateddate` must be either missing or no later than `as_of`. Results are
filtered by `lengthreport` for the requested frequency and deduplicated by
`(yearreport, lengthreport)`, retaining the newest eligible publication/revision.
`quarterly` accepts only codes `1`–`4`, `annual` accepts only code `5`, and the
page `limit` is bounded to `1`–`20`; cumulative codes `6` and `9` are not mixed
into standalone quarters in v1. Rows with status `2` are never used as an
automatic historical fallback.
Because the source stores the latest row rather than a complete revision ledger,
the result remains `point_in_time_quality=partial` when an older revision cannot
be reconstructed. Missing database values remain `null`; they are never changed
to zero.

For ordinary companies (`comtypecode=CT`), the canonical `normalized` cash-flow
fields follow the supplied **Navisoft dictionary dated 2024-12-26**. The mapping
is dictionary-backed rather than inferred from the opaque `cfa*` column names;
the original source fields and statement metadata remain available in each row:

| Normalized field | Navisoft source field |
| --- | --- |
| `net_cash_from_operating_activities` | `cfa18` |
| `capital_expenditures` | `cfa19` |
| `net_cash_from_investing_activities` | `cfa26` |
| `net_cash_from_financing_activities` | `cfa34` |
| `net_change_in_cash` | `cfa35` |
| `cash_and_cash_equivalents_beginning` | `cfa36` |
| `foreign_exchange_effect` | `cfa37` |
| `cash_and_cash_equivalents_ending` | `cfa38` |
| `free_cash_flow` | `cfa18 + cfa19` |

`capital_expenditures` preserves the source sign; the adapter does not convert a
negative purchase outflow to a positive magnitude. `free_cash_flow` is computed
only when both `cfa18` and `cfa19` are present, using `cfa18 + cfa19`; otherwise
it remains `null`. This CT mapping is not applied to banks (`NH`), securities
firms (`CK`) or insurers (`BH`) until their authoritative dictionaries are
supplied. The adapter does not infer a monetary unit from the workbook.

The existing GX Analysis HTTP endpoint does not expose cash flow yet. Therefore,
with `GX_DATA_TRANSPORT=api`, `get_cashflow` reports `NOT_MODELED`/unavailable
until the GX backend endpoint is extended. The GX profile deliberately does not
fall back to Yahoo for cash flow in either transport, so an API gap or database
error cannot silently mix a current-only Yahoo statement into a point-in-time GX
run. Use PostgreSQL explicitly when cash flow is required.

Use a database role with `CONNECT` plus `SELECT` only. The adapter rejects a DSN
whose connected database name differs from `GX_MARKET_INFO_EXPECTED_DB`, uses
parameterized read queries, and applies a statement timeout. Prefer the HTTP API
for developer laptops and production services unless direct database access is a
deliberate network/security decision.

Run `doctor` before a paid/full run. It makes no LLM request; it checks LLM
configuration and asks the GX adapter for the last trading session plus recent HPG
daily candles.

Common failures:

- `401/403`: key or GX token is missing/invalid; check the profile loaded by
  `--env-file`.
- `404 model not found`: the hosted model is unavailable or the Ollama tag was not
  pulled; compare against `ollama list`.
- Connection refused: GX/Ollama is not running, or `localhost` is being resolved
  inside a container. Use `host.docker.internal` from Docker on macOS.
- Tool loop or empty analyst report: use a tool-capable model and verify the
  OpenAI-compatible server implements tool calls.
- Context/OOM: use the same 8B model for quick and deep, reduce selected analysts,
  article limits and debate rounds.
- Provider mismatch on resume: a session deliberately rejects LLM or transport
  changes. Start a new run rather than mixing outputs from different runtimes.

## Vietnam retail-social sentiment

The GX profile routes retail-social evidence to FireAnt and disables the
StockTwits/Reddit path used by upstream/international symbols. CafeF/VnExpress
remain news/media evidence (`media_tone`); they are not relabelled as retail
social discussion. See [vietnam-social-sentiment.md](vietnam-social-sentiment.md)
for authorization, archive schema v2, collector, 15:15 snapshot, PIT/retention
limits and CLI operations. FireAnt is fail-closed by default, and unavailable
evidence is never converted into a neutral signal.

CafeF/VnExpress RSS are a separate editorial-media lane used by News and
`media_tone`. They are authorization-locked, archive-only during analyst stages,
and never treated as FireAnt retail social. See
[vietnam-editorial-media.md](vietnam-editorial-media.md) for install, fixed feed
allowlist, encrypted archive, collector/purge commands and point-in-time rules.

## Vietnam macro evidence

GX News reads official NSO/NSDP and SBV observations from a separate local
strict-PIT archive; it never calls FRED, even if `FRED_API_KEY` is present. The
upstream/international profile keeps its existing FRED behavior. Install
`.[vn-macro]`, run `macro collect --once`, then inspect `macro status` or
`macro show --as-of YYYY-MM-DD --json`. `doctor` remains offline unless
`--live-macro` is supplied. Full source mappings, status/staleness rules and
scheduling examples are in [vietnam-macro.md](vietnam-macro.md).

- On macOS, if an editable install succeeds but `tradingagents-gx` raises
  `ModuleNotFoundError`, inspect `ls -lO .venv`. A recursively inherited
  `hidden` flag can make Python skip the editable `.pth`; clear it with
  `chflags -R nohidden .venv`, then repeat the editable install. This workaround
  is only needed when that flag is present.

TradingAgents produces research output, not executable brokerage orders. Keep GX
credentials read-only and treat the final decision as advisory.
