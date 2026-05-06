# Hermes Agent Medical Research Fork

> *"Turns Hermes into something I'd actually want running during a busy shift — fetches real PubMed articles, PDF guidelines, and full journal articles without getting blocked or cutting the answer short. Give it a complex clinical question and it pulls 10+ sources, reads them properly, and writes a structured answer that respects the depth the topic deserves. For evidence-based acute care research at the end of a long night, this does what a good medical library tool should."*
>
> — **89/100** · *Claude (free tier), acting as a satisfied reviewer*

**Last fork README.md update:** 2026-05-07

## Nanobot Medical Research Fork → Hermes Port Status

| Feature | Nanobot | Status | Note |
|---------|---------|--------|------|
| **Tiered Web Fetcher** | ✅ curl_cffi + Scrapling | ✅ Fork | P0 |
| **PDF Extraction** | ✅ PyMuPDF | ✅ Fork | P0 |
| **Reddit JSON API** | ✅ Auto-convert | ✅ Fork | P0 |
| **Force-Final Threshold** | ✅ max_iter - 2 | ✅ Fork | P0 |
| **Tool Summary Display** | ✅ CLI only | ✅ Fork | P0 |
| **Max Iterations (200)** | ✅ Default | ✅ Fork | P0 |
| **max_tool_result_chars (400K)** | ✅ | ✅ Fork | P0 |
| **SearXNG Search** | ✅ Hardcoded URL | ✅ Upstream | `web.search_backend: searxng` |
| **Disabled LLM Summarization 5K-500K** | ✅ | ✅ Fork | Returns raw text |
| **MAX_OUTPUT_SIZE (10K)** | ✅ | ✅ Fork | Upstream: 5K |
| **web_extract threshold (500K)** | ✅ | ✅ Fork | Upstream: 100K |
| **WhatsApp Channel** | ✅ | ❌ Native | Hermes handles natively |
| **Commands (/s, /c, /rerun)** | ✅ | ❌ SKIP | Hermes has prefix matching |
| **ExecTool timeout (90s)** | ✅ | ❌ Config | `code_execution.timeout` |
| **context_window_tokens (200K)** | ✅ | ❌ Config | `model.context_length` |
| **ReadFileTool limits** | ✅ | ❌ Config | `file_read_max_chars` |
| **_CHAT_RETRY_DELAYS** | ✅ 5 attempts | ❌ SKIP | Hermes: 3 retries, jittered |

## Fork Changes (19 customizations)

### Web Tools (`tools/web_tools.py`)
- **Tiered Local Fetcher**: curl_cffi (Chrome TLS) → Scrapling (JS/Cloudflare) → httpx fallback
- **`web.extract_backend: local`**: Bypass cloud APIs entirely for extraction
- **Per-capability backend split**: Upstream added `web.search_backend` / `web.extract_backend` separation; fork adds `local` as extract backend with smart fallback
- **SearXNG**: Upstream native support for `web.search_backend: searxng` (set `SEARXNG_URL` in env)
- **PDF & HTML**: PyMuPDF for PDFs, trafilatura for HTML-to-text
- **Reddit**: `.json` auto-conversion, structured parsing
- **LLM summarization disabled** for 5K–500K range (returns raw text)
- **MAX_OUTPUT_SIZE = 10,000** (upstream: 5,000)
- **`web_extract` max_result_size_chars = 500,000** (upstream: 100,000)

### Agent (`run_agent.py`)
- **max_iterations = 200** (upstream: 90)
- **Force-final threshold** at N-2 (prevents infinite tool loops)
- **Tool summary tracking** + `result['tool_summary']` key (CLI streaming display)

### Budget (`tools/budget_config.py`)
- **DEFAULT_RESULT_SIZE_CHARS = 400,000** (upstream: 100,000)
- **DEFAULT_TURN_BUDGET_CHARS = 500,000** (upstream: 200,000)

### CLI & Gateway
- **CLI/Gateway defaults** aligned to 200 iterations
- **Tool summary in streaming mode** (printed after stream box closes)

## What You Can Do Now (setting up for medical research)

- **Tell Hermes to** : pull this fork into local Hermes agent ( https://github.com/rdnot/hermes-agent-medical-research/ )

- **Restart Hermes agent** : `/exit` then `hermes chat` (in CLI), `/restart` (in messaging app)

- **Tell Hermes to** : set SOUL.md to
```
I am Hermes agent, a helpful AI assistant for ER doctor.
Personality
Helpful and in-depth.
Concise and to the point.
Do not ask follow up questions.
Curious and eager to learn, easy to trigger web_search tool if user asks for information.
Always list relevant URL references at the end of response.
## Values
Accuracy over speed
User privacy and safety
Transparency in actions
```

- **Tell Hermes to** : set USER.md to
```
User Profile
Role: Emergency Room (ER) Doctor
Communication style: Medical assistant style
Timezone: ***
Language: English
```

- **Tell Hermes to** : set MEMORY.md to
```
## Current Long-term Memory
## User Information
* User is an ER doctor who uses AI tools for in-depth medical research, improving patient care.
* User's work context involves processing medical literature, guidelines, and analysing time-critical evidence-based acute care of ER patients.

## Preferences
* User has a strong preference for verified and latest up-to-date sources.
* User is knowledgeable about current medical guidelines and will correct inaccuracies when found.
* **CRITICAL: For medical knowledge summaries, user requires comprehensive, structured format with practical ER clinical points.**

## Important Notes
* NEVER fabricate or invent search results. If information is not current, say so honestly.
* Always list relevant URL references at the end of response.
```

- **Tell Hermes to** : set web.search_backend to tavily (or searxng), and set the api key in env. For free local search → set web.search_backend to searxng and set SEARXNG_URL in env, or tell Hermes to install and set up local searxng)
- **Tell Hermes to** : set web.extract_backend to local (fork: free extraction via curl_cffi → scrapling → httpx, with fallback to web.backend on rare total failure)
  [or set in config yourself]
```yaml
web:
  backend: tavily              # shared fallback for both search and extract
  search_backend: tavily       # specify search provider (tavily | searxng | exa | parallel | firecrawl)
  # search_backend: searxng    # (optional) override search to use free self-hosted searxng (needs SEARXNG_URL in .env)
  extract_backend: local       # fork: free local extraction (curl_cffi → scrapling → httpx → fallback to web.backend)
  # extract_backend: firecrawl # use Firecrawl directly (no local attempt)
  # extract_backend: tavily    # use Tavily directly
```

**Backend resolution logic:**

| Config `extract_backend` | Behavior | On failure |
|---|---|---|
| `local` | curl_cffi → scrapling → httpx | Falls back to `web.backend` (if non-empty), else error |
| `firecrawl` | Firecrawl API directly (no local attempt) | Error |
| `tavily` / `exa` / `parallel` | That API directly | Error |
| `searxng` | Error (search-only, cannot extract) | — |
| `""` (empty) | Falls to `web.backend`, then auto-detect from env | Error |

- **Tell Hermes to** : install required dependencies (curl_cffi, scrapling, scrapling[fetchers], trafilatura, PyMuPDF(optional)) then install the browser dependencies with `scrapling install`)

- **Restart Hermes agent** : `/exit` then `hermes chat` (in CLI), `/restart` (in messaging app)

- **Tell Hermes to** : do 1 web search then 1 local web extraction about pubmed pneumonia article then summarize

### ✅ Ready for Testing

Your comprehensive research use case should work:

"Comprehensive research about pneumonia in ER , fetch at least 10 up-to-date, evidence-based and reliable sources or guidelines, make it into .md file in ~/workspace folder, write 2000 words first then edit for more contents."

**Expected Output:**
- ✅ ~7000 words
- ✅ ~50KB size
- ✅ ~28 tool calls
- ✅ Structured markdown
- ✅ Tool summary displayed

---

<p align="center">
  <img src="assets/banner.png" alt="Hermes Agent" width="100%">
</p>

# Hermes Agent ☤

<p align="center">
  <a href="https://hermes-agent.nousresearch.com/docs/"><img src="https://img.shields.io/badge/Docs-hermes--agent.nousresearch.com-FFD700?style=for-the-badge" alt="Documentation"></a>
  <a href="https://discord.gg/NousResearch"><img src="https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/NousResearch/hermes-agent/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://nousresearch.com"><img src="https://img.shields.io/badge/Built%20by-Nous%20Research-blueviolet?style=for-the-badge" alt="Built by Nous Research"></a>
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/Lang-中文-red?style=for-the-badge" alt="中文"></a>
</p>

**The self-improving AI agent built by [Nous Research](https://nousresearch.com).** It's the only agent with a built-in learning loop — it creates skills from experience, improves them during use, nudges itself to persist knowledge, searches its own past conversations, and builds a deepening model of who you are across sessions. Run it on a $5 VPS, a GPU cluster, or serverless infrastructure that costs nearly nothing when idle. It's not tied to your laptop — talk to it from Telegram while it works on a cloud VM.

Use any model you want — [Nous Portal](https://portal.nousresearch.com), [OpenRouter](https://openrouter.ai) (200+ models), [NVIDIA NIM](https://build.nvidia.com) (Nemotron), [Xiaomi MiMo](https://platform.xiaomimimo.com), [z.ai/GLM](https://z.ai), [Kimi/Moonshot](https://platform.moonshot.ai), [MiniMax](https://www.minimax.io), [Hugging Face](https://huggingface.co), OpenAI, or your own endpoint. Switch with `hermes model` — no code changes, no lock-in.

<table>
<tr><td><b>A real terminal interface</b></td><td>Full TUI with multiline editing, slash-command autocomplete, conversation history, interrupt-and-redirect, and streaming tool output.</td></tr>
<tr><td><b>Lives where you do</b></td><td>Telegram, Discord, Slack, WhatsApp, Signal, and CLI — all from a single gateway process. Voice memo transcription, cross-platform conversation continuity.</td></tr>
<tr><td><b>A closed learning loop</b></td><td>Agent-curated memory with periodic nudges. Autonomous skill creation after complex tasks. Skills self-improve during use. FTS5 session search with LLM summarization for cross-session recall. <a href="https://github.com/plastic-labs/honcho">Honcho</a> dialectic user modeling. Compatible with the <a href="https://agentskills.io">agentskills.io</a> open standard.</td></tr>
<tr><td><b>Scheduled automations</b></td><td>Built-in cron scheduler with delivery to any platform. Daily reports, nightly backups, weekly audits — all in natural language, running unattended.</td></tr>
<tr><td><b>Delegates and parallelizes</b></td><td>Spawn isolated subagents for parallel workstreams. Write Python scripts that call tools via RPC, collapsing multi-step pipelines into zero-context-cost turns.</td></tr>
<tr><td><b>Runs anywhere, not just your laptop</b></td><td>Seven terminal backends — local, Docker, SSH, Singularity, Modal, Daytona, and Vercel Sandbox. Daytona and Modal offer serverless persistence — your agent's environment hibernates when idle and wakes on demand, costing nearly nothing between sessions. Run it on a $5 VPS or a GPU cluster.</td></tr>
<tr><td><b>Research-ready</b></td><td>Batch trajectory generation, Atropos RL environments, trajectory compression for training the next generation of tool-calling models.</td></tr>
</table>

---

## Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
```

Works on Linux, macOS, WSL2, and Android via Termux. The installer handles the platform-specific setup for you.

> **Android / Termux:** The tested manual path is documented in the [Termux guide](https://hermes-agent.nousresearch.com/docs/getting-started/termux). On Termux, Hermes installs a curated `.[termux]` extra because the full `.[all]` extra currently pulls Android-incompatible voice dependencies.
>
> **Windows:** Native Windows is not supported. Please install [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install) and run the command above.

After installation:

```bash
source ~/.bashrc    # reload shell (or: source ~/.zshrc)
hermes              # start chatting!
```

---

## Getting Started

```bash
hermes              # Interactive CLI — start a conversation
hermes model        # Choose your LLM provider and model
hermes tools        # Configure which tools are enabled
hermes config set   # Set individual config values
hermes gateway      # Start the messaging gateway (Telegram, Discord, etc.)
hermes setup        # Run the full setup wizard (configures everything at once)
hermes claw migrate # Migrate from OpenClaw (if coming from OpenClaw)
hermes update       # Update to the latest version
hermes doctor       # Diagnose any issues
```

📖 **[Full documentation →](https://hermes-agent.nousresearch.com/docs/)**

## CLI vs Messaging Quick Reference

Hermes has two entry points: start the terminal UI with `hermes`, or run the gateway and talk to it from Telegram, Discord, Slack, WhatsApp, Signal, or Email. Once you're in a conversation, many slash commands are shared across both interfaces.

| Action | CLI | Messaging platforms |
|---------|-----|---------------------|
| Start chatting | `hermes` | Run `hermes gateway setup` + `hermes gateway start`, then send the bot a message |
| Start fresh conversation | `/new` or `/reset` | `/new` or `/reset` |
| Change model | `/model [provider:model]` | `/model [provider:model]` |
| Set a personality | `/personality [name]` | `/personality [name]` |
| Retry or undo the last turn | `/retry`, `/undo` | `/retry`, `/undo` |
| Compress context / check usage | `/compress`, `/usage`, `/insights [--days N]` | `/compress`, `/usage`, `/insights [days]` |
| Browse skills | `/skills` or `/<skill-name>` | `/<skill-name>` |
| Interrupt current work | `Ctrl+C` or send a new message | `/stop` or send a new message |
| Platform-specific status | `/platforms` | `/status`, `/sethome` |

For the full command lists, see the [CLI guide](https://hermes-agent.nousresearch.com/docs/user-guide/cli) and the [Messaging Gateway guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging).

---

## Documentation

All documentation lives at **[hermes-agent.nousresearch.com/docs](https://hermes-agent.nousresearch.com/docs/)**:

| Section | What's Covered |
|---------|---------------|
| [Quickstart](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart) | Install → setup → first conversation in 2 minutes |
| [CLI Usage](https://hermes-agent.nousresearch.com/docs/user-guide/cli) | Commands, keybindings, personalities, sessions |
| [Configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration) | Config file, providers, models, all options |
| [Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging) | Telegram, Discord, Slack, WhatsApp, Signal, Home Assistant |
| [Security](https://hermes-agent.nousresearch.com/docs/user-guide/security) | Command approval, DM pairing, container isolation |
| [Tools & Toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools) | 40+ tools, toolset system, terminal backends |
| [Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills) | Procedural memory, Skills Hub, creating skills |
| [Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) | Persistent memory, user profiles, best practices |
| [MCP Integration](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp) | Connect any MCP server for extended capabilities |
| [Cron Scheduling](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron) | Scheduled tasks with platform delivery |
| [Context Files](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files) | Project context that shapes every conversation |
| [Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture) | Project structure, agent loop, key classes |
| [Contributing](https://hermes-agent.nousresearch.com/docs/developer-guide/contributing) | Development setup, PR process, code style |
| [CLI Reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands) | All commands and flags |
| [Environment Variables](https://hermes-agent.nousresearch.com/docs/reference/environment-variables) | Complete env var reference |
| [Web Search](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search) | SearXNG, Firecrawl, Tavily, Exa — per-capability config |

---

## Migrating from OpenClaw

If you're coming from OpenClaw, Hermes can automatically import your settings, memories, skills, and API keys.

**During first-time setup:** The setup wizard (`hermes setup`) automatically detects `~/.openclaw` and offers to migrate before configuration begins.

**Anytime after install:**

```bash
hermes claw migrate              # Interactive migration (full preset)
hermes claw migrate --dry-run    # Preview what would be migrated
hermes claw migrate --preset user-data   # Migrate without secrets
hermes claw migrate --overwrite  # Overwrite existing conflicts
```

What gets imported:
- **SOUL.md** — persona file
- **Memories** — MEMORY.md and USER.md entries
- **Skills** — user-created skills → `~/.hermes/skills/openclaw-imports/`
- **Command allowlist** — approval patterns
- **Messaging settings** — platform configs, allowed users, working directory
- **API keys** — allowlisted secrets (Telegram, OpenRouter, OpenAI, Anthropic, ElevenLabs)
- **TTS assets** — workspace audio files
- **Workspace instructions** — AGENTS.md (with `--workspace-target`)

See `hermes claw migrate --help` for all options, or use the `openclaw-migration` skill for an interactive agent-guided migration with dry-run previews.

---

## Contributing

We welcome contributions! See the [Contributing Guide](https://hermes-agent.nousresearch.com/docs/developer-guide/contributing) for development setup, code style, and PR process.

Quick start for contributors — clone and go with `setup-hermes.sh`:

```bash
git clone https://github.com/NousResearch/hermes-agent.git
cd hermes-agent
./setup-hermes.sh     # installs uv, creates venv, installs .[all], symlinks ~/.local/bin/hermes
./hermes              # auto-detects the venv, no need to `source` first
```

Manual path (equivalent to the above):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv venv --python 3.11
source venv/bin/activate
uv pip install -e ".[all,dev]"
scripts/run_tests.sh
```

> **RL Training (optional):** The RL/Atropos integration (`environments/`) ships via the `atroposlib` and `tinker` dependencies pulled in by `.[all,dev]` — no submodule setup required.

---

## Community

- 💬 [Discord](https://discord.gg/NousResearch)
- 📚 [Skills Hub](https://agentskills.io)
- 🐛 [Issues](https://github.com/NousResearch/hermes-agent/issues)
- 🔌 [HermesClaw](https://github.com/AaronWong1999/hermesclaw) — Community WeChat bridge: Run Hermes Agent and OpenClaw on the same WeChat account.

---

## License

MIT — see [LICENSE](LICENSE).

Built by [Nous Research](https://nousresearch.com).
