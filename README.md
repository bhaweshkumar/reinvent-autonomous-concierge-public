# re:Invent Autonomous Concierge

Planning a week at AWS re:Invent means more than finding interesting talks. Sessions fill,
venues are spread across Las Vegas, and a schedule that looks open on a screen can be
impossible to cross in time. The re:Invent Autonomous Concierge brings those decisions into
one local-first workflow: search the catalog in plain English, account for estimated travel,
compare explainable itinerary alternatives, and keep watch for seats that open on waitlisted
sessions.

The concierge connects to AI assistants through the Model Context Protocol (MCP), and includes
an optional Streamlit interface for search, schedules, transit and waitlists. It is built for
attendees who want an assistant to help plan and manage a conference schedule, and for
developers exploring practical MCP tools with explicit safety boundaries.

In live attendee mode, the application and its state run on your machine; the AWS Events API
is the only remote service the concierge itself needs. The language model is supplied by the
MCP client you choose. A separate hosted demo is available for trying the experience without
sign-in or real schedule changes.

> **It must run on the attendee's own machine.** AWS Builder ID sign-in only accepts
> redirects to `http://127.0.0.1` (or `localhost`) on ports 8484–8489, and AWS offers no
> hosted redirect URI. So each attendee installs and runs the concierge locally, signs
> themselves in, and the concierge acts only for them. The [hosted demo](#6-hosted-read-only-demo-spec-21)
> is a separate, simulated experience with no sign-in and no AWS Events writes. See
> [Architecture](#architecture).

## What it does

| Capability                        | Details                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Hybrid catalog search             | SQLite filters + keyword rank + semantic rank (vectors in SQLite, cosine ranking in numpy), merged with Reciprocal Rank Fusion (under 150 ms locally)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Preference-led itinerary planning | Keep an event-scoped preference profile locally, generate immutable alternatives from the local catalog and a current schedule snapshot, then compare plans and inspect why sessions were selected or excluded                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| Safe plan application             | Preview the proposed schedule changes first. Applying a plan requires an explicit, short-lived approval tied to the plan and schedule state; writes are journaled and checked against AWS afterward                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Transit-aware scheduling          | NetworkX graph of the Las Vegas venues from the packaged registry (`src/reinvent_concierge/data/venues.json`); Caesars Forum and Caesars Palace are separate venues. Required minutes add inside-venue walking, floor and zone crossings to the inter-venue time, scaled by the profile's transit multiplier; every number is labeled an estimate. An unrecognized venue never blocks a plan — it lowers confidence (`estimate_quality: LOW`) instead. A reservable session gets an arrive-10-minutes-early buffer (`RESERVED_SEAT_LEAD_MINUTES`); a feasible but under-10-minute margin is flagged `tight`. Adds `Transit: A → B` personal-time blocks for gaps the attendee can't trust to self-manage |
| Venue from the room               | Public payloads carry no venue, so it is read from the `room` string through the registry (floor and repeat group are parsed too). A first room segment the registry does not know, such as `Expo`, is stored as the session's `zone` with venue `Unknown`                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Repeat-aware planning             | Sessions that share a `repeat_group` (same code with a `-R`, `-R1`… suffix) are one offering of the same content; a plan selects at most one of them                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Event time                        | All times are stored in UTC and shown in the event timezone, read from `GetEvent` at sync. Search results and schedules carry `start_local` / `end_local` next to the UTC values                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Booking with approval-gated swaps | Reserves a session when nothing must be released and falls back to favorites when writes are disabled or reservations are unavailable. A time overlap or unreachable neighbour returns `CONFLICT_DETECTED` with a proposed swap and a single-use approval token; calling again with the token executes it. Must-attend sessions are never released                                                                                                                                                                                                                                                                                                                                                       |
| Seat snapper                      | Background worker that polls the waitlist in priority order and reserves seats only (never cancels). A clash records `SWAP_PROPOSED` and keeps the entry for attendee review                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| MCP server                        | 23 tools over stdio (Claude Desktop) or SSE (web UI)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Web UI                            | Chat search, agenda calendar with transit blocks, waitlist control center                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Safety                            | Schedule writes are disabled by default and explicitly opt-in. Destructive changes require approval. Tokens are stored with mode `0600` and never logged; the append-only audit log stores only a SHA-256 digest                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |

## From search to a schedule

The planning workflow separates recommendations from actions that change an AWS schedule:

1. **Sync the catalog.** Session data and semantic-search vectors are stored locally. Run a
   full sync after installation; later syncs update changed sessions.
2. **Set preferences.** Maintain a local profile for the event, including interests,
   commitments and planning constraints. Suggested profile changes can be reviewed before
   saving.
3. **Generate and compare plans.** The planner uses the local catalog and a fresh read of the
   attendee's schedule to build feasible alternatives. Plans are saved locally and include
   deterministic scores and explanations for selected sessions and notable exclusions.
4. **Preview before applying.** Plan generation does not change the AWS schedule. Review the
   proposed changes, then explicitly approve the preview to apply it. If the schedule changes
   after preview, the approval is rejected and the plan must be reviewed again.
5. **Monitor waitlisted sessions.** The seat snapper can reserve an available seat when doing
   so needs no existing reservation to be released. If a swap is needed, it keeps the waitlist
   entry and asks the attendee to review the proposed change.

Transit times and inferred room locations are estimates, not indoor navigation or a guarantee
that a route is feasible. The concierge does not register attendees, guarantee seats, or
automatically cancel a reservation. Every destructive schedule change requires attendee
approval.

## At a glance

- [Quick start](#quick-start) and [requirements](#requirements)
- [Architecture and trust boundaries](#architecture)
- [Running the MCP server, UI, planner and seat snapper](#running)
- [Hosted read-only demo](#6-hosted-read-only-demo-spec-21)
- [Configuration and MCP tools](#configuration)
- [Development, tests and troubleshooting](#development)

## Architecture

The live system is deliberately local: the attendee's OS account contains the credentials,
catalog and planning state, while AWS remains the source of truth for the event and schedule.
These C4 diagrams show that boundary at two levels:

**C4 Level 1: System context**

[System context diagram](./docs/diagrams/c4-context.puml)

**C4 Level 2: Live containers**

[Live container diagram](./docs/diagrams/c4-container-live.puml)

The `.puml` links above point to the canonical C4-PlantUML sources. They can be checked without
generating image files with
`plantuml -checkonly docs/diagrams/*.puml`.

## Requirements

- Python 3.10+ (developed on 3.13), macOS or Linux
- An AWS Builder ID registered for re:Invent 2026
- A browser on the same machine (sign-in redirects to a loopback port) and one free port in
  8484–8489
- The default `semantic` extra (fastembed/ONNX) adds about 75 MB on disk and ~230 MB resident
  after the model loads. The `semantic-torch` extra (sentence-transformers/PyTorch, kept one
  release for anyone pinned to it) adds about 570 MB on disk and ~600 MB resident — see
  [Install footprint](#install-footprint) below. The first semantic sync downloads the
  `all-MiniLM-L6-v2` embedding model (~90 MB) under either extra.

## Install footprint

Measured on macOS/arm64, Python 3.13, `all-MiniLM-L6-v2`, one warm-up embed call (SPEC-20 §20.4
AC1). `semantic` (fastembed/ONNX) is the default; `semantic-torch` (sentence-transformers/PyTorch)
is kept one release for anyone pinned to the old stack and installed instead, not alongside it.

| Extra                                            | On-disk size | RSS after model load |
| ------------------------------------------------ | ------------ | -------------------- |
| `semantic` (fastembed + onnxruntime)             | ~75 MB       | ~230 MB              |
| `semantic-torch` (sentence-transformers + torch) | ~570 MB      | ~600 MB              |

FAISS is removed outright (SPEC-20 §20.2): vectors live in a `session_vectors` SQLite table,
ranked with a plain numpy dot product, since the full set is small enough (≤ 5k × 384 float32 ≈
8 MB) for a brute-force scan to stay well under the 150 ms search target.

## Quick start

```bash
git clone https://github.com/bhaweshkumar/reinvent-autonomous-concierge.git
cd reinvent-autonomous-concierge
make install                    # creates .venv and runs: pip install -e ".[dev,semantic,ui]"
source .venv/bin/activate

cp .env.example .env            # optional: override defaults (see Configuration)
concierge login                 # AWS Builder ID sign-in in your browser; tokens saved with mode 0600
concierge doctor --mcp          # verify API, sign-in, registration, catalog and hosted MCP
concierge sync --full           # download and index the catalog
```

Then pick how you want to use it:

```bash
concierge serve                           # MCP over stdio, for Claude Desktop
concierge serve --transport sse           # MCP over SSE on http://127.0.0.1:8000/sse
concierge ui                              # web UI on http://127.0.0.1:8501 (needs the SSE server)
concierge snapper                         # seat snapper worker (Ctrl-C / SIGTERM to stop)
concierge run                             # coordinated catalog sync + seat snapper runtime
```

`python -m reinvent_concierge <command>` works the same as `concierge <command>`.

### Connecting to the AWS Events API

[ListEvents and GetEvent] need no credentials. The re:Invent
catalog and your schedule need an AWS Builder ID sign-in, and you must be registered for
the event.

```bash
concierge login               # opens the Builder ID sign-in page (--no-browser prints the URL)
concierge doctor              # checks each layer and tells you what to fix
concierge doctor --mcp        # ...and the hosted MCP server at https://api.awsevents.com/mcp
concierge logout              # revokes the refresh token and deletes stored tokens
```

Run `login` on the machine where your browser is: the browser delivers the authorization
code to a loopback port, so signing in over SSH or inside a remote container does not work.

It listens on `http://127.0.0.1:<port>/callback`, using the first free reserved port from 8484 to 8489,
and checks the `state` value it gets back. It then stores the access token (valid 60
minutes) and the refresh token (30 days). After a `401`, the client refreshes the token
once and retries. The same token works for the hosted MCP server, which uses the same
authorization server and the `events/access` scope.

If you already have a token from another tool, run `concierge store-token
--with-refresh-token` instead.

`doctor` reports one line per check:

```
OK   API reachable: ListEvents: 1 events
OK   Event: reinvent2026: re:Invent 2026 (registration required)
OK   Token stored: /Users/you/.aws-events/credentials
OK   Signed in and registered: GetSchedule: 0 reserved, 3 favorites, 0 personal time
OK   Catalog readable: first session AIM101
OK   Write access: dry-run (WRITE_ACCESS_ACTIVE=false)
OK   Hosted MCP server: 12 tools
OK   Catalog mapping current: 1234 sessions
```

A `403` means the Builder ID isn't registered for the event. A `401` means you need to
run `concierge login` again.

## Running

### 1. Keep the catalog fresh

```bash
concierge sync            # delta sync: re-embeds only changed sessions, marks removed ones CANCELLED
concierge sync --full     # rebuild the catalog and the search index
```

The MCP tools search the local catalog, so run `sync` before searching.

### 2. MCP clients (Claude Desktop, Claude Code, Kiro)

**Claude Desktop** — add the `reinvent-concierge` entry from
[mcp-configs/claude-desktop.json](mcp-configs/claude-desktop.json) to your Claude Desktop
config, replacing the placeholder with the absolute path to this checkout:

```json
{
  "mcpServers": {
    "reinvent-concierge": {
      "command": "/ABSOLUTE/PATH/TO/reinvent-autonomous-concierge/.venv/bin/concierge",
      "args": ["serve", "--transport", "stdio"]
    }
  }
}
```

Restart Claude Desktop and ask, for example: _"Find advanced Bedrock sessions at the
Venetian and book the best one."_

**Claude Code** — the same concierge server, plus (optionally) the hosted AWS Events MCP
server with the callback port AWS documents for Claude Code:

```bash
claude mcp add --scope user reinvent-concierge -- \
  /ABSOLUTE/PATH/TO/reinvent-autonomous-concierge/.venv/bin/concierge serve --transport stdio
claude mcp add --transport http --scope user awsevents https://api.awsevents.com/mcp \
  --callback-port 8484 --client-id 7vmom55m1qstvq8i71ph127bfq
```

**Kiro** — [mcp-configs/kiro.json](mcp-configs/kiro.json) configures the hosted AWS Events MCP
server with Kiro's registered callback (`http://127.0.0.1:8976`, port only — Kiro adds the
path itself).

The hosted MCP server handles its own sign-in, separately from `concierge login`. AWS documents
registered callbacks for Kiro and Claude Code only; the `awsevents` entry in
`claude-desktop.json` reuses Kiro's values and is untested with Claude Desktop. If its sign-in
fails with `redirect_mismatch`, use Claude Code or Kiro for the hosted server.

### 3. Web UI

The UI calls the MCP server over SSE, so run both in separate terminals:

```bash
make serve-sse    # terminal 1: concierge serve --transport sse
make ui           # terminal 2: concierge ui, then open http://127.0.0.1:8501
```

| Tab              | What it does                                                                                                                                                    |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Chat Concierge   | Search in plain English. Each result card has Book / Favorite / Waitlist buttons with inline transit feedback                                                   |
| Agenda & Transit | Day-by-hour calendar, color-coded by venue, with hatched transit blocks. Click a session for Book / Cancel / Waitlist. Also shows the venue transit-time matrix |
| Waitlist Control | Priority, auto-snag toggle, remove, and live status (refreshes every 10 s), plus recent snapper outcomes                                                        |

### 4. Seat snapper

```bash
concierge snapper               # polls every 60 s (SNAPPER_POLL_INTERVAL_SECONDS)
concierge snapper --interval 30
```

Each cycle reloads the waitlist (the `waitlist_entries` table in the planner database) and
refreshes seat availability for each entry, then
reserves open seats in priority order (1 = highest) through the approval service, and sends
a desktop notification. It only reserves; it never cancels.

- A time clash with an existing reservation records `SWAP_PROPOSED` status (no token; the
  attendee gets a fresh token via **Review swap** in the UI), keeps the entry, and notifies
  once per transition.
- HTTP 409 (reservations closed) keeps the entry and defers reservations for the rest of the
  cycle.
- HTTP 429 keeps the entry `WATCHING`; the snapper has no backoff of its own, the quota
  governor paces writes.
- After a reservation, neighbours the new session makes unreachable are reported as
  `TRANSIT_CONFLICT`, never canceled.
- `concierge snapper` runs under the runtime lease, so it cannot run alongside
  `concierge run`.
- Rolling back to a release before SPEC-17 drops waitlist entries in `SWAP_PROPOSED` status
  (older versions don't recognise it).

The worker finishes its current cycle before exiting on SIGTERM.

> Legacy concierge transit blocks are recognized, and may be
> replaced once (REMOVE + CREATE, approval-gated) when a new plan is applied.

### 5. Coordinated background runtime

```bash
concierge run                                      # snapper interval from configuration; catalog every 300 s
concierge run --poll-interval 30 --catalog-interval 600
```

`concierge run` is the preferred owner of recurring catalog sync and seat-snapper work.
It schedules both jobs independently, so a failed cycle is logged without stopping the
other job. Only one runtime for an event can run on a machine at a time. On macOS and
Linux, an owner-only advisory lock enforces this; platforms without that lock use an
event-scoped SQLite lease renewed every 15 seconds and expired after 30 seconds. A second
runtime exits with `Runtime already running for event 'reinvent2026'`. Stopping the
runtime with Ctrl-C or SIGTERM waits for active cycles to finish and releases the lease.
After a crash, start `concierge run` again; the process lock is released by the OS, or the
SQLite lease becomes available after its expiry.

### 6. Hosted read-only demo (SPEC-21)

A separate, internet-facing mode for judges/visitors to try search, preference profiles and
itinerary plans against a **simulated** schedule — no AWS Events sign-in exists, and no AWS
Events write is possible.

```bash
make demo-serve                 # local try-out: profile=DEMO on http://127.0.0.1:8080
```

- `create_app_context(profile="DEMO")` never constructs a `CredentialManager` or an
  authenticated `AWSEventsRestClient`. Catalog reads go through `DemoCatalogClient`
  (`src/reinvent_concierge/demo/catalog_client.py`), hard-allowlisted to anonymous
  `GET /events` and `GET /events/{id}/sessions*`; every schedule read/write goes through
  `SimulatedScheduleClient` (`demo/simulated_schedule.py`), entirely in memory, with
  deterministic per-session outcomes.
- Each visitor gets their own `AppContext` and planner database
  (`/tmp/demo/<uuid>.db`, default idle expiry 30 min, default cap 50 concurrent visitors —
  `DEMO_SESSION_IDLE_MINUTES`, `DEMO_MAX_CONCURRENT_SESSIONS`); nothing is shared between them
  except the read-only baked catalog.
- Streamlit (`ui/demo_app.py`) connects to an in-process FastMCP server over fastmcp's
  in-memory transport — no network MCP endpoint is exposed, matching the live UI's `MCPClient`
  (which already accepts either a URL or a `FastMCP` instance).
- `concierge demo serve` binds `0.0.0.0:8080` by default (unlike `ui`/`serve`, which stay on
  loopback): this mode is meant to be reached from outside localhost.

#### Deploying the demo to AWS

**Deploying it is a separate, explicit step** — nothing in this repository deploys anything
on its own; every command below is one you run yourself when you choose to.

**Prerequisites:**

| Tool            | Why                                                                                                   | Check                                               |
| --------------- | ----------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| Docker          | builds the image, both for local testing and (via `DockerImageAsset`) during `cdk synth`/`cdk deploy` | `docker info`                                       |
| Node.js         | the `aws-cdk-lib` Python package itself shells out to a bundled Node process                          | `node --version`                                    |
| `aws-cdk` CLI   | drives `cdk synth`/`cdk deploy`; npm-distributed, not pip                                             | `npm install -g aws-cdk` → `cdk --version`          |
| AWS credentials | an AWS CLI profile with permission to create the resources below                                      | `aws sts get-caller-identity --profile bk_personal` |

**Steps, in order:**

1. `make demo-snapshot` — syncs a read-only catalog+vectors snapshot into
   `infra/demo-snapshot/` and records the event in `infra/demo-snapshot/event_id`. Which event
   is set by `DEMO_EVENT_ID`:
   - **default `reinvent2026`** — the registration-required catalog; run `concierge login` as a
     registered attendee first;
   - **a public event, for tests** — `make demo-snapshot DEMO_EVENT_ID=<public-event-id>`; no
     sign-in needed (`curl https://api.awsevents.com/v1/events` lists events and whether each
     requires registration).

   `make demo-image` and `cdk synth`/`cdk deploy` build the image for the recorded event (Docker
   build argument `AWS_EVENTS_EVENT_ID`), so the running demo always matches its snapshot.
   Everything after this step reads that snapshot; nothing here touches the real AWS Events
   write API.

2. `make demo-synth` — builds `infra/.venv` if needed, then `cdk synth`: builds the Docker
   image locally (so `infra/demo-snapshot/` must already exist — step 1), renders the
   CloudFormation template, and prints it. **Deploys nothing.** Use this to sanity-check the
   stack before committing to a deploy.
3. **One-time per AWS account/region:** `cd infra && AWS_PROFILE=bk_personal cdk bootstrap`
   — provisions the CDK toolkit resources (an S3 asset bucket, the ECR asset repo
   `DockerImageAsset` stages through, IAM roles) that every `cdk deploy` depends on.
4. `make demo-deploy` — the actual deploy: builds the image, pushes it into a repository
   the stack owns (`cdk-ecr-deployment`, not CDK's own bootstrap-managed asset repo — that
   one can't have its pull policy granted to a third party like Lightsail, confirmed by
   inspecting a real `cdk synth` output, not assumed), creates the Lightsail container
   service, grants it pull access, and sets up the $20/month AWS Budgets alarm and the
   14-day CloudWatch log group.

Both `demo-synth` and `demo-deploy` use the `bk_personal` AWS CLI profile by default
(`make demo-deploy AWS_PROFILE=other-profile` to override). `infra/cdk.json`'s `"app"`
command points straight at `infra/.venv/bin/python3`, so `cdk` always resolves the venv's
interpreter regardless of your shell's `PATH`.

**Tearing it down:** `cd infra && AWS_PROFILE=bk_personal cdk destroy` removes the Lightsail
service, the owned ECR repository (`empty_on_delete=True`, so images are deleted with it),
the budget and the log group. It does not run `cdk bootstrap`'s resources back out — those
are shared across any stack in the account/region and are left in place.

### Behavior change (SPEC-17)

No tool or worker cancels a reservation without an approval token. Previously
`optimize_and_book_session` and the seat snapper cancelled sessions a new booking made
unreachable; they now propose a swap (or report `TRANSIT_CONFLICT`) and wait for an attendee
approval. Approval tokens are single-use, expire after 5 minutes, are bound to the schedule
fingerprint, and are redacted from the audit log. Only `booking/` and `api/` call client write
methods (enforced by `tests/unit/test_write_path_boundary.py`).

### Reservation write access

`WRITE_ACCESS_ACTIVE=false` is the default. While it is false, no reservation, cancellation or
personal-time write is sent to AWS; booking requests become favorites, the snapper only watches,
and audit entries are prefixed `[DRY_RUN]`. Set `WRITE_ACCESS_ACTIVE=true` only when you intend
to enable schedule writes. AWS may still refuse reservations when the event's reservation
window is closed.

## Configuration

Settings are resolved in this order (highest first): environment variables,
`~/.aws-events/config.json`, a `.env` file in the working directory, then the defaults.
Keys are the variable names below, in upper or lower case.

| Variable                                                    | Default                            | Purpose                                                                                                                                                                                                                   |
| ----------------------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `WRITE_ACCESS_ACTIVE`                                       | `false`                            | Enables reservations, cancellations and personal-time writes                                                                                                                                                              |
| `AWS_EVENTS_EVENT_ID`                                       | `reinvent2026`                     | Event to sync and act on                                                                                                                                                                                                  |
| `AWS_EVENTS_API_BASE_URL`                                   | `https://api.awsevents.com/v1`     | REST API base URL                                                                                                                                                                                                         |
| `EVENT_TIMEZONE`                                            | `America/Los_Angeles`              | Fallback zone, used only until the first sync records the event's zone from `GetEvent` (an invalid zone name is ignored)                                                                                                  |
| `AWS_EVENTS_MCP_SERVER_URL`                                 | `http://127.0.0.1:8000/sse`        | SSE endpoint the UI connects to                                                                                                                                                                                           |
| `API_RATE_LIMIT_RPS`                                        | `5`                                | Outgoing request limit per second                                                                                                                                                                                         |
| `EMBEDDING_MODEL`                                           | `all-MiniLM-L6-v2`                 | fastembed (ONNX) model id for semantic search; vectors live in `LOCAL_DB_PATH`'s `session_vectors` table, keyed by this id                                                                                                |
| `QUOTA_DEFAULT_TIMEOUT_SECONDS`                             | `60.0`                             | How long an AWS Events API call waits for quota headroom before giving up                                                                                                                                                 |
| `RESERVED_SEAT_LEAD_MINUTES`                                | `10`                               | Minutes before a reservable session's start by which re:Invent is assumed to release unclaimed reserved seats; added to the transit deadline for that session (confirm before the event)                                  |
| `SNAPPER_POLL_INTERVAL_SECONDS`                             | `60`                               | Seat snapper cycle length                                                                                                                                                                                                 |
| `LOCAL_DB_PATH`                                             | `~/.aws-events/session_catalog.db` | SQLite catalog and semantic vectors                                                                                                                                                                                       |
| `PLANNER_DB_PATH`                                           | `~/.aws-events/planner_state.db`   | Owner-only SQLite state: snapshots, apply journals, shared operation quotas and runtime leases; never stores tokens                                                                                                       |
| `WAITLIST_PATH`                                             | `~/.aws-events/waitlist.json`      | Legacy waitlist file; imported into `PLANNER_DB_PATH` once at startup and renamed to `.migrated`. The live waitlist is the `waitlist_entries` table                                                                       |
| `AUDIT_LOG_PATH`                                            | `~/.aws-events/audit_log.json`     | Legacy JSON-lines audit file; imported into `PLANNER_DB_PATH` once at startup and renamed to `.migrated`. The live audit trail is the `audit_events` table (`concierge audit export` writes JSON-lines from it on demand) |
| `AWS_EVENTS_CREDENTIALS_PATH`                               | `~/.aws-events/credentials`        | Token file (mode `0600`)                                                                                                                                                                                                  |
| `AWS_EVENTS_OAUTH_TOKEN_URL` / `AWS_EVENTS_OAUTH_CLIENT_ID` | AWS Events values                  | Token refresh endpoint and public client ID                                                                                                                                                                               |

### AWS operation quotas

The application has two separate controls for AWS traffic. `API_RATE_LIMIT_RPS` is a
short-burst, in-process token bucket that limits all outgoing HTTP requests. The planner
database also tracks AWS's documented per-attendee, rolling-minute operation quotas across
the CLI, UI, MCP server, and workers. A `429` blocks the matching operation until the
server's `Retry-After` interval passes. These controls complement each other: changing
`API_RATE_LIMIT_RPS` does not increase an AWS operation quota.

| Operation                                                                                                     | Limit per rolling minute | Accounted unit      |
| ------------------------------------------------------------------------------------------------------------- | -----------------------: | ------------------- |
| `GetSession`, `ListSessions`                                                                                  |                      120 | request             |
| `GetSchedule`                                                                                                 |                       60 | request             |
| `ReserveSessions`, `AssociateFavorites`                                                                       |                       30 | distinct session ID |
| `CancelReservation`, `DisassociateFavorite`, `CreatePersonalTime`, `UpdatePersonalTime`, `DeletePersonalTime` |                       30 | request             |

### Search-index recovery

Vectors live in `LOCAL_DB_PATH`'s `session_vectors` table, one row per session keyed by
`(model_id, text_hash)`. A seat-only change never touches a session's embedded text, so it never
re-embeds; a title/description/track/speaker change does. If the `semantic` extra isn't
installed, search degrades to keyword-only (`concierge doctor` reports this) and recovers the
next time a sync runs with the extra present — no corrupt-file recovery step is needed, since a
SQLite write either commits or doesn't.

After upgrading from a version that used FAISS, run `concierge sync --full` once to populate
`session_vectors`; the old `faiss.index`/`.rows.json`/`.manifest.json` files are no longer read
and can be deleted.

## MCP tools

| Tool                                                                                                 | Purpose                                                                                                                                                                                                                                                                                                                                                           |
| ---------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `search_catalog(query, level?, venue?, hero_only?, limit=5, session_type?, topic?)`                  | Hybrid search over open sessions (`limit` 1–20). `venue` is matched by canonical venue, so an alias such as `Palazzo` finds sessions stored under either name; `session_type` (for example `Workshop`) and `topic` filter on the mapped taxonomy fields                                                                                                           |
| `get_my_schedule(include_favorites?, include_personal_time?)`                                        | Reserved sessions, favorites, personal time                                                                                                                                                                                                                                                                                                                       |
| `optimize_and_book_session(session_id, auto_resolve_conflicts=True, apply_run_id?, approval_token?)` | Returns `CONFIRMED`, `FAVORITED`, `CONFLICT_DETECTED` or `FAILED`. On a conflict it returns `proposed_changes`, `apply_run_id`, `approval_token` and `expires_at` (no token when a must-attend session blocks); call again with `apply_run_id` and `approval_token` to execute the swap. `auto_resolve_conflicts` now only adds transit buffers and never cancels |
| `manage_favorites(action, session_id)`                                                               | `add` or `remove`                                                                                                                                                                                                                                                                                                                                                 |
| `check_transit(from_session_id, to_session_id)`                                                      | Transit minutes, gap, feasibility, deficit, plus `margin_minutes`, `estimate_quality` (`NORMAL`/`LOW`) and `tight` (feasible with under 10 minutes to spare)                                                                                                                                                                                                      |
| `add_to_waitlist(session_id, priority=50)`                                                           | Queue a full session for the snapper                                                                                                                                                                                                                                                                                                                              |
| `list_waitlist()`                                                                                    | List every queued entry, by priority then age                                                                                                                                                                                                                                                                                                                     |
| `update_waitlist_entry(session_id, priority?, enabled?)`                                             | Change a waitlist entry's priority and/or auto-snag toggle                                                                                                                                                                                                                                                                                                        |
| `remove_from_waitlist(session_id)`                                                                   | Remove one entry from the waitlist                                                                                                                                                                                                                                                                                                                                |
| `get_recent_outcomes(limit=10)`                                                                      | Latest snapper outcomes: reservations, swap proposals, transit warnings, throttling                                                                                                                                                                                                                                                                               |
| `cancel_reservation(session_id, apply_run_id?, approval_token?)`                                     | Two-step: the first call returns `APPROVAL_REQUIRED` with a token (valid 5 minutes, single use); the second call with `apply_run_id` and `approval_token` returns `CANCELED`. Must-attend sessions are refused. Used by the UI's Cancel action                                                                                                                    |

The UI is a thin MCP client: it reads and changes the waitlist and recent outcomes through
these tools, never through the waitlist store or audit log directly.

Session results include `start_local` and `end_local` (ISO 8601 with the event's offset) next to the UTC `start_time` and `end_time`. `get_my_schedule` also returns `event_timezone`, the IANA zone those local strings use.

Every call is written to the audit log, including failures.

## Using it as a library

```python
import asyncio

from reinvent_concierge.app_context import create_app_context


async def main() -> None:
    context = await create_app_context()  # config, API client, catalog, agent, ...
    try:
        await context.ingestion.incremental_sync()
        results = await context.ingestion.search(
            "generative AI Bedrock", level=300, venue="Venetian", limit=5
        )
        for session in results:
            print(session.session_id, session.title, session.seat_availability)
    finally:
        await context.aclose()


asyncio.run(main())
```

## Project layout

```
reinvent-autonomous-concierge/
├── pyproject.toml              # dependencies, `concierge` entry point, ruff/mypy/pytest config
├── Makefile                    # install / run / test / lint shortcuts
├── .env.example                # sample settings
├── Dockerfile                  # hosted demo image (SPEC-21); built by `make demo-image`
├── infra/                      # AWS CDK stack for the hosted demo (SPEC-21); not deployed here
├── mcp-configs/                # Claude Desktop and Kiro MCP client configs
├── docs/
│   ├── diagrams/                # C4 architecture diagrams (PlantUML source)
│   ├── plantuml-icons/          # shared PlantUML defaults
├── src/reinvent_concierge/
│   ├── cli.py                  # `concierge` commands
│   ├── auth.py                 # Builder ID sign-in (OAuth code + PKCE) and sign-out
│   ├── doctor.py               # connectivity checks behind `concierge doctor`
│   ├── app_context.py          # composition root: builds and wires every service
│   ├── runtime.py              # runtime lease + catalog-sync/seat-snapper scheduling
│   ├── legacy_migration.py     # one-time JSON -> planner-database import (SPEC-18)
│   ├── config.py               # pydantic-settings configuration
│   ├── credentials.py          # 0600 token storage
│   ├── models.py               # domain models
│   ├── exceptions.py
│   ├── audit.py                # audit trail (planner database `audit_events` table)
│   ├── waitlist.py             # waitlist store (planner database `waitlist_entries` table)
│   ├── api/                    # AWS Events REST client, wire→domain mapping, rate limiter,
│   │                           #   ports (protocols api/ consumers implement), hosted-MCP client
│   ├── ingestion/              # catalog sync + hybrid search (SQLite, numpy vectors, RRF)
│   ├── demo/                   # SPEC-21: allowlisted anonymous catalog client, simulated schedule
│   ├── transit/                # venue graph and feasibility checks
│   ├── schedule/                # conflict detection, transit buffers, seat swaps
│   ├── booking/                 # the one write path: journaled, quota-governed, approval-gated
│   ├── planner/                 # preferences, itinerary planning, plan application, state store
│   ├── agent/                  # MCP tool implementations + FastMCP server
│   ├── snapper/                # seat snapper worker
│   └── ui/                     # Streamlit app (+ demo_app.py, SPEC-21), MCP client, view models
└── tests/
    ├── unit/                   # one module per component
    ├── integration/            # end-to-end workflows over mocked HTTP (respx)
    ├── live/                   # opt-in tiers against the real API and hosted MCP server
    ├── factories.py            # test data builders
    └── conftest.py             # isolated config / credentials fixtures
```

### Module layering

Imports flow one way (checked by `tests/unit/test_layering.py`):

```text
cli, app_context, runtime        (composition and process)
  → agent, snapper, ui           (surfaces)
    → booking, planner           (application services)
      → schedule, ingestion, transit
        → api, demo              (wire + ports; demo is api's anonymous/simulated sibling)
          → models, config, exceptions, util
```

Only `cli` and `app_context` wire concrete services together. `api/mapping.py` is the only
code that knows the API's wire format, and `api/ports.py` holds the protocols `api/`
depends on (so `api/client.py` never imports downward into `planner`). The UI talks to the
waitlist and recent outcomes only through MCP tools, never through `waitlist.py` or
`audit.py` directly.

## Development

```bash
make check              # ruff lint + format check, mypy, full test suite
make test               # all tests
make test-unit          # unit tests
make test-integration   # end-to-end workflows over mocked HTTP
make test-cov           # coverage report (htmlcov/)
make format             # ruff --fix + ruff format
```

The default tests are offline and never touch `~/.aws-events`. Every test gets its own
temporary config, credentials and data files.

### Live tests against the real API

`tests/live/` checks every feature against the real AWS Events API and the hosted MCP
server. It is excluded from `make test` and runs in tiers you opt into.

```bash
make test-live                                   # public catalog (no sign-in) + signed-in read-only + hosted MCP
make test-live-writes                            # + favorites / personal time / transit buffers, cleaned up afterwards
LIVE_ALLOW_RESERVATIONS=1 make test-live-writes  # + reserve/cancel (AWS may return 409 while reservations are closed)
```

| Variable                                | Effect                                                                                     |
| --------------------------------------- | ------------------------------------------------------------------------------------------ |
| _(none)_                                | Public-catalog tier only. Signed-in tiers skip with "run `concierge login`"                |
| token from `concierge login`            | Signed-in read-only tier and hosted MCP parity                                             |
| `LIVE_ALLOW_WRITES=1`                   | Schedule writes. Each test undoes its own changes, and leftovers are swept at the end      |
| `LIVE_ALLOW_RESERVATIONS=1`             | Reserve, then cancel straight away. Only uses sessions that don't clash with your bookings |
| `LIVE_TEST_REFRESH=1`                   | Forces a token refresh (rotates your stored refresh token)                                 |
| `LIVE_REAL_EMBEDDINGS=1`                | Uses the real embedding model and checks the < 150 ms search target                        |
| `LIVE_EVENT_ID`, `LIVE_PUBLIC_EVENT_ID` | Pick the events to test (defaults: `reinvent2026`, newest public event)                    |
| `AWS_EVENTS_ACCESS_TOKEN`               | Use this token instead of `~/.aws-events/credentials`                                      |

## Data and security

- All state lives under `~/.aws-events/`: catalog, index, planner database (waitlist and
  audit trail included), credentials.
- The token file is created with mode `0600`. Tokens never appear in logs, errors or the
  audit log, which stores `sha256(token)` only.
- `planner_state.db` and its parent directory are created with modes `0600` and `0700`.
  They contain planner metadata only: tokens and authorization headers are rejected before
  journal persistence.
- HTTP redirects are not followed, so the bearer token is only sent to the configured API host.
- The SSE server and the UI bind to `127.0.0.1` by default. `concierge serve --transport sse`
  refuses a non-loopback `--host` unless `--allow-remote` is given, and otherwise rejects a
  forged `Host` header (DNS rebinding) and a cross-origin browser request.

## Troubleshooting

| Symptom                                                          | Fix                                                                                                                                                                                                          |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `No token found … Run: concierge login`                          | Sign in. Tools that read your schedule need a token                                                                                                                                                          |
| `AuthenticationRequiredError` (401)                              | The token expired and couldn't be refreshed. Run `concierge login`                                                                                                                                           |
| 403 from the API                                                 | Your Builder ID isn't registered for the event                                                                                                                                                               |
| `concierge login` says all callback ports are in use             | Free one of the ports 8484–8489 (often another MCP client)                                                                                                                                                   |
| `concierge login` times out over SSH or in a container           | Sign in on the machine where the browser runs; the callback is a loopback port                                                                                                                               |
| `concierge logout` prints "(revocation failed)"                  | Local tokens are deleted anyway. The refresh token stays valid on the server until it expires (30 days); end the Builder ID session at profile.aws.amazon.com                                                |
| `cdk synth` fails: `infra/demo-snapshot/event_id is missing`     | Run `make demo-snapshot` (snapshots from before the `DEMO_EVENT_ID` flag have no marker)                                                                                                                     |
| Not sure which layer is broken                                   | Run `concierge doctor --mcp`                                                                                                                                                                                 |
| `Catalog mapping current … run concierge sync` fails in `doctor` | The local catalog was synced before mapping v2 (no `venue_source`), so venue filters and transit see old data. Run `concierge sync` (or `--full`) once                                                       |
| `CERTIFICATE_VERIFY_FAILED`                                      | The CLI already uses your OS trust store, which covers TLS-inspecting proxies such as Zscaler. If the proxy's root CA isn't installed in the OS either, set `SSL_CERT_FILE` to a PEM bundle that contains it |
| UI shows "Cannot reach the concierge server"                     | Start `concierge serve --transport sse` and check `AWS_EVENTS_MCP_SERVER_URL`                                                                                                                                |
| Searches return nothing                                          | Run `concierge sync --full`. Search only returns sessions with status `OPEN`                                                                                                                                 |
| Bookings come back as `FAVORITED`                                | Dry-run mode is active: set `WRITE_ACCESS_ACTIVE=true` only if you intend to enable schedule writes                                                                                                          |
