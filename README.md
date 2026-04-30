# ARIA — Autonomous Resident Intelligence Agent

ARIA is an autonomous AI agent that lives on your home machine (Raspberry Pi 5 or any Linux/Windows box) and is controlled entirely through Telegram. It browses the web, runs shell commands, remembers things across sessions, schedules recurring tasks, and writes new tools for itself when it encounters something it can't do yet.

---

## Install

Paste this one line into your terminal on any Linux machine (Raspberry Pi, Ubuntu, Debian, etc.):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/YG99999/ARIA/main/install.sh)
```

That's it. The script will:
- Install git, Python 3.10+, and all system libraries if they're missing
- Clone the repo
- Create a Python virtual environment and install all dependencies
- Download Chromium for browser automation
- Add an `aria` command to your PATH
- Launch the setup wizard where you enter your Telegram token and API key

The whole thing takes 2–5 minutes on a Pi, under a minute on a fast machine. At the end you'll have a running ARIA.

> **You need two things before running the installer:**
> 1. A Telegram bot token — create one free at [@BotFather](https://t.me/botfather)
> 2. An LLM API key — [OpenRouter](https://openrouter.ai) works with any model, including free ones

---

## What it can do

- **Browse the web** — fill forms, log in, extract data, interact with any site
- **Run shell commands** — install packages, manage files, run scripts, call APIs
- **Remember you** — facts, preferences, and past tasks persist across restarts
- **Schedule anything** — "every morning at 8, check my Stripe dashboard"
- **Write its own tools** — when no existing tool covers a task, it writes Python inline and promotes successful approaches to permanent skills
- **Stay safe** — asks before spending money, sending messages, deleting files, or modifying itself. All sensitive actions require your explicit approval.

---

## Running

After the one-line install:

```bash
aria          # start normally
aria --setup  # re-run the setup wizard
```

Or with systemd (the wizard offers to set this up):
```bash
systemctl start aria
systemctl enable aria   # auto-start on boot
```

### Manual install (Windows / skip the script)

```bash
git clone https://github.com/YG99999/ARIA.git
cd ARIA
pip install -r aria/requirements.txt
playwright install chromium
python aria/main.py --setup
```

### SearXNG (optional — enables web search)

```bash
docker run -d --name searxng -p 8080:8080 searxng/searxng
```

Without this, ARIA still works for all browser and shell tasks. `search_web` just won't be available.

---

## Telegram commands

### Core
| Command | What it does |
|---------|-------------|
| `/status` | Current tasks (idle or working on what) |
| `/tasks` | List all tasks |
| `/task [id]` | Full detail for a task — every step |
| `/stop [id]` | Cancel a running task |
| `/agents` | Active workers and queue position |

### Memory
| Command | What it does |
|---------|-------------|
| `/memory` | All stored facts |
| `/recall [term]` | Search facts, history, and sessions — labels `[fact]` / `[history]` / `[session]` |
| `/docs` | Ingested documents |
| `/doc forget [name]` | Delete a document from memory |
| `/session` | Active session info |

### Observability
| Command | What it does |
|---------|-------------|
| `/journal [n\|term]` | Last N journal entries, or search by term |
| `/health` | CPU, RAM, disk, temp, LLM reachability |
| `/logs [n]` | Raw Python log file tail |
| `/why [id]` | Plain-English narrative of what a task did |
| `/trust` | Recent prompt-injection detection events |
| `/dream` | Last dream cycle — what was consolidated, flagged, pruned |

### Scheduling
| Command | What it does |
|---------|-------------|
| `/schedule` | List scheduled jobs |
| `/pause` | Pause task dispatch |
| `/resume` | Resume dispatch |
| `/briefing [on\|off] [hour]` | Enable/disable morning briefing (default off) |

### Budget
| Command | What it does |
|---------|-------------|
| `/budget` | Today's and this month's LLM spend vs limits |

### Skills
| Command | What it does |
|---------|-------------|
| `/skills` | All known skills and their health |
| `/skill [name]` | Detail for one skill |
| `/skill [name] disable\|enable` | Toggle a skill |

### Approvals
| Command | What it does |
|---------|-------------|
| `/approvals` | Pending approval requests |
| `/approve [id]` | Approve an action |
| `/deny [id]` | Deny an action |

### Advanced
| Command | What it does |
|---------|-------------|
| `/retry [id] [step]` | Restart a task from a saved checkpoint |
| `/rollback [n]` | Git revert last N commits and restart ARIA |
| `/accounts` | Saved credentials (no secrets shown) |
| `/captcha [on\|off]` | Toggle CapSolver CAPTCHA solving |
| `/webhooks` | Registered webhook sources and Tailscale URL |
| `/handover` | VNC handover status and URL (Linux only) |
| `/mode [beginner\|expert]` | Switch UX verbosity |
| `/backup` | Download a zip of `data/` + `skills/` |

---

## Configuration

### `.env`

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_TOKEN` | Yes | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | Your personal chat ID |
| `LLM_API_KEY` | Yes | API key for your LLM provider |
| `LLM_BASE_URL` | Yes | OpenAI-compatible endpoint (e.g. `https://openrouter.ai/api/v1`) |
| `BROWSER_HEADLESS` | No | `true` / `false` — default `true` on Linux, `false` on Windows |
| `CAPSOLVER_KEY` | No | CapSolver API key for CAPTCHA solving |
| `TAILSCALE_IP` | No | Your machine's Tailscale IP for VNC handover links |
| `WEBHOOK_SECRET_GITHUB` | No | HMAC secret for GitHub webhook verification |
| `WEBHOOK_SECRET_STRIPE` | No | HMAC secret for Stripe webhook verification |

### `config/models.yaml`

Controls which models ARIA uses for fast (routing, summarization) vs. heavy (reasoning, coding, browser) tasks, and sets the budget limits:

```yaml
models:
  - id: anthropic/claude-haiku-4-5
    tier: light          # fast model
  - id: anthropic/claude-sonnet-4-5
    tier: heavy          # smart model

budget:
  daily_usd_alert: 2.00
  daily_usd_hard_stop: 10.00
  monthly_usd_hard_stop: 50.00
```

Any OpenRouter model ID works. The `tier: light` model is used for routing, summarization, session management, and the dream cycle. The `tier: heavy` model handles all agent reasoning, coding, and browser tasks.

### `config/persona.md`

ARIA's personality and behaviour instructions. Edit freely, or tell ARIA to "update your persona to be more formal" and it will rewrite the file itself.

---

## Architecture

```
Telegram (sole interface)
        │
        ▼
  Orchestrator (5s poll loop)
        │
        ├── Router (fast model) → classifies: browser / shell / mixed / conversation
        │
        ├── WorkerAgent ──► WorkerSpec (tool-filtered sandbox)
        │       │               ├── shell_spec  → run_shell_command, execute_python, file tools
        │       │               ├── browser_spec → browse (browser-use Agent), computer_use
        │       │               └── dev_spec    → write→test→fix loop
        │       │
        │       ├── ReAct loop with structured OpenAI function calling
        │       ├── Context compression at token threshold
        │       └── Step snapshots → agent_log (used by /retry)
        │
        ├── MemoryRetriever → injected last in every system prompt
        │       ├── Facts (high-importance always included)
        │       ├── Skills meta (relevant to query)
        │       ├── History (top 3 relevant past tasks)
        │       ├── Doc chunks (FTS5 over ingested documents)
        │       └── Active session (4-hour grouping)
        │
        ├── ApprovalQueue → asyncio.Event per request, Telegram inline buttons
        │
        ├── APScheduler (BackgroundScheduler, SQLite jobstore)
        │       ├── Health snapshot every 5 min
        │       ├── Journal compaction 3:00am
        │       ├── Chromium cleanup 3:00am
        │       ├── Dream cycle 3:30am
        │       └── Morning briefing (off by default)
        │
        └── Webhook receiver (aiohttp :8765, HMAC-verified, EXTERNAL-tagged)
```

### Three SQLite databases

| DB | Contains |
|----|---------|
| `state.db` | Tasks, agent_log, scratch, credentials, approvals, schedule, health, llm_usage, paused |
| `memory.db` | Facts, history, sessions, skills_meta, doc_chunks — all with FTS5 shadow tables |
| `journal.db` | Append-only event log (thoughts, actions, errors, system events) with FTS5 |

All databases use WAL mode + NORMAL sync. Schema is version-gated and idempotent.

### Trust model

Every piece of content entering an LLM prompt is tagged by source:

| Tag | Source |
|-----|--------|
| `<owner>` | Your Telegram messages |
| `<aria_memory>` | ARIA's own facts, history, skills, journal |
| `<system_output>` | Tool results, shell stdout/stderr |
| `<external>` | Web content, API responses, ingested documents |

The LLM is explicitly told never to treat `<external>` content as instructions. Suspected prompt-injection signals are logged to journal and viewable via `/trust`.

### Skills system

When ARIA completes a task in 4+ steps, it uses an LLM to extract a reusable skill and saves it to `skills/`. Skills are loaded at startup (each in its own `try/except` — one bad skill never prevents startup) and injected into the agent's context when relevant.

After every task, skill success/failure counts are updated. A skill auto-disables after 2 consecutive failures. The nightly dream cycle reviews skill health and flags degraded ones.

### Dream cycle (3:30am daily)

Six steps, each isolated in `try/except`:
1. **Memory consolidation** — find patterns in the last 7 days, promote to facts
2. **Skill health review** — flag skills with more failures than successes
3. **Contradiction detection** — find conflicting facts, queue a user clarification
4. **Session summarization** — summarize sessions older than 7 days
5. **Self-assessment** — 3–5 sentence reflection on what went well / badly
6. **Clutter pruning** — delete stale inferred facts and unused skill metadata

---

## Browser notes

### Two browser paths

| Path | Stealth | When to use |
|------|---------|-------------|
| `browse` tool (browser-use Agent) | **No** | Standard sites — forms, logins, data extraction |
| `create_browser()` factory | **Yes** (playwright-stealth at context level) | Bot-sensitive sites — aggressive Cloudflare, Akamai, Datadome |

The `browse` tool uses browser-use's internal Playwright context which we do not patch with stealth. This is a documented, accepted limitation — browser-use 0.1.40 does not expose a context-creation hook we can reliably intercept. For highly bot-sensitive sites, route the task through a custom tool built on `create_browser()`.

### Sessions

ARIA saves Playwright session state after successful logins. On the next task for the same service, it loads the saved session instead of re-authenticating. Raw credentials are never injected into LLM prompts — they go directly to the browser at the moment of use.

### CAPTCHA solving

Disabled by default. To enable:
1. Add `CAPSOLVER_KEY=...` to `.env`
2. Send `/captcha on` in Telegram

Supports reCAPTCHA v2/v3, hCaptcha, and Cloudflare Turnstile.

---

## Security

- **One authorized user** — all Telegram messages from any other chat ID are silently dropped
- **Approval required** for: spending money, sending messages, deleting files, publishing content, modifying ARIA's own code
- **Shell sandboxed** on Linux via `aria-agent` restricted user (no sudo, limited write access)
- **Fernet encryption** for stored credentials — key at `data/.secrets_key`, never in `.env`, excluded from `/backup`
- **Prompt injection protection** — web content is tagged `<external>` and the LLM is instructed to ignore instructions inside those tags. Detected signals are logged and shown in `/trust`

---

## Development

```bash
# Run import smoke test
python aria/test_imports.py

# Run integration tests (no Telegram or LLM connection needed)
python aria/test_integration.py
```

The integration tests cover all 15 core subsystems with in-process SQLite (temp directories). Expected output: `46/46 passed`.

### File layout

```
aria/
├── main.py                  # Entry point + graceful shutdown
├── setup.py                 # Onboarding wizard
├── requirements.txt
├── core/
│   ├── config.py            # Settings singleton
│   ├── database.py          # DB connections + migrations
│   ├── tools.py             # @tool decorator registry
│   ├── orchestrator.py      # Main loop + dispatch
│   ├── router.py            # Task classification
│   ├── tasks.py             # TaskRegistry CRUD
│   ├── approval.py          # Approval queue + asyncio Events
│   ├── skill_extractor.py   # Post-task skill generation
│   ├── context_builder.py   # Trust tagging + injection detection
│   ├── llm_client.py        # Centralised LLM calls with backoff
│   ├── cost_guard.py        # Budget enforcement
│   └── secrets.py           # Fernet encryption wrapper
├── agents/
│   ├── base.py              # WorkerSpec, BaseAgent, context compression
│   ├── worker.py            # WorkerAgent — single runtime for all specs
│   └── specs.py             # browser_spec(), shell_spec(), dev_spec()
├── tools/                   # All @tool-decorated functions
├── memory/                  # Facts, history, sessions, skills, retriever
├── scheduler/               # APScheduler wrapper
├── tg/                      # Telegram bot, commands, notify rate-limiter
├── system/                  # Journal, health, handover, dreamer, webhook
├── skills/                  # ARIA writes here at runtime
└── config/
    ├── models.yaml
    ├── agent_modes.yaml
    ├── persona.md
    └── .env.example
```

---

## Pinned dependencies

| Package | Version | Note |
|---------|---------|------|
| `browser-use` | `==0.1.40` | Do not auto-update — minor bumps have broken the Agent API |
| `playwright` | `==1.47.0` | Matched to browser-use |
| `playwright-stealth` | `==1.0.6` | Applied at context level only — per-page API broken in this version |
| `apscheduler` | `==3.10.4` | Uses BackgroundScheduler (not AsyncIOScheduler) to avoid blocking the event loop |

---

## License

MIT
