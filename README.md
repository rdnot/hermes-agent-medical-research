# Hermes Agent Medical Research Fork

> *\"Turns Hermes into something I'd actually want running during a busy shift — fetches real PubMed articles, PDF guidelines, and full journal articles without getting blocked or cutting the answer short. Give it a complex clinical question and it pulls 10+ sources, reads them properly, and writes a structured answer that respects the depth the topic deserves. For evidence-based acute care research at the end of a long night, this does what a good medical library tool should.\"*\n>
> — **89/100** · *Claude (free tier), acting as a satisfied reviewer*

**Last fork README.md update:** 2026-05-07

## Nanobot → Hermes Port Status

| Feature | Nanobot | Status | Note |
|---------|---------|--------|------|\| **Tiered Web Fetcher** | ✅ curl_cffi + Scrapling | ✅ Fork | P0 |
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
- **Per-capability backend split**: Upstream added `web.search_backend` / `web.extract_backend` separation; fork preserves `local` as valid extract backend
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

## Setup for Medical Research

```bash\n# 1. Clone this fork\ngit clone https://github.com/rdnot/hermes-agent-medical-research.git ~/.hermes/hermes-agent\ncd ~/.hermes/hermes-agent\nsource venv/bin/activate  # or use the installer\n\n# 2. Configure web backends\nhermes config set web.backend tavily          # or: firecrawl, exa, searxng\nhermes config set web.extract_backend local    # fork: use free local fetcher\n\n# 3. Install fork dependencies\npip install curl_cffi scrapling trafilatura PyMuPDF\nscrapling install  # browser deps for JS rendering\n\n# 4. (Optional) Set up SearXNG for free local search\n# Set SEARXNG_URL in ~/.hermes/.env, then:\nhermes config set web.search_backend searxng\n```

### Config example (`~/.hermes/config.yaml`)

```yaml\nweb:\n  backend: tavily              # search backend (or searxng for free)\n  search_backend: searxng       # optional: override search separately\n  extract_backend: local         # fork: free local extraction\n```

---

<p align="center">\n  <img src="assets/banner.png" alt="Hermes Agent" width="100%">\n</p>

# Hermes Agent ☤

<p align="center">\n  <a href="https://hermes-agent.nousresearch.com/docs/"><img src="https://img.shields.io/badge/Docs-hermes--agent.nousresearch.com-FFD700?style=for-the-badge" alt="Documentation"></a>\n  <a href="https://discord.gg/NousResearch"><img src="https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>\n  <a href="https://github.com/NousResearch/hermes-agent/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>\n  <a href="https://nousresearch.com"><img src="https://img.shields.io/badge/Built%20by-Nous%20Research-blueviolet?style=for-the-badge" alt="Built by Nous Research"></a>\n  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/Lang-中文-red?style=for-the-badge" alt="中文"></a>\n</p>

**The self-improving AI agent built by [Nous Research](https://nousresearch.com).** It's the only agent with a built-in learning loop — it creates skills from experience, improves them during use, nudges itself to persist knowledge, searches its own past conversations, and builds a deepening model of who you are across sessions. Run it on a $5 VPS, a GPU cluster, or serverless infrastructure that costs nearly nothing when idle. It's not tied to your laptop — talk to it from Telegram while it works on a cloud VM.

Use any model you want — [Nous Portal](https://portal.nousresearch.com), [OpenRouter](https://openrouter.ai) (200+ models), [NVIDIA NIM](https://build.nvidia.com) (Nemotron), [Xiaomi MiMo](https://platform.xiaomimimo.com), [z.ai/GLM](https://z.ai), [Kimi/Moonshot](https://platform.moonshot.ai), [MiniMax](https://www.minimax.io), [Hugging Face](https://huggingface.co), OpenAI, or your own endpoint. Switch with `hermes model` — no code changes, no lock-in.

<table>\n<tr><td><b>A real terminal interface</b></td><td>Full TUI with multiline editing, slash-command autocomplete, conversation history, interrupt-and-redirect, and streaming tool output.</td></tr>\n<tr><td><b>Lives where you do</b></td><td>Telegram, Discord, Slack, WhatsApp, Signal, and CLI — all from a single gateway process. Voice memo transcription, cross-platform conversation continuity.</td></tr>\n<tr><td><b>A closed learning loop</b></td><td>Agent-curated memory with periodic nudges. Autonomous skill creation after complex tasks. Skills self-improve during use. FTS5 session search with LLM summarization for cross-session recall. <a href="https://github.com/plastic-labs/honcho">Honcho</a> dialectic user modeling. Compatible with the <a href="https://agentskills.io">agentskills.io</a> open standard.</td></tr>\n<tr><td><b>Scheduled automations</b></td><td>Built-in cron scheduler with delivery to any platform. Daily reports, nightly backups, weekly audits — all in natural language, running unattended.</td></tr>\n<tr><td><b>Delegates and parallelizes</b></td><td>Spawn isolated subagents for parallel workstreams. Write Python scripts that call tools via RPC, collapsing multi-step pipelines into zero-context-cost turns.</td></tr>\n<tr><td><b>Runs anywhere, not just your laptop</b></td><td>Seven terminal backends — local, Docker, SSH, Singularity, Modal, Daytona, and Vercel Sandbox. Daytona and Modal offer serverless persistence — your agent's environment hibernates when idle and wakes on demand, costing nearly nothing between sessions. Run it on a $5 VPS or a GPU cluster.</td></tr>\n<tr><td><b>Research-ready</b></td><td>Batch trajectory generation, Atropos RL environments, trajectory compression for training the next generation of tool-calling models.</td></tr>\n</table>

---

## Quick Install

```bash\ncurl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash\n```

Works on Linux, macOS, WSL2, and Android via Termux. The installer handles the platform-specific setup for you.

> **Android / Termux:** The tested manual path is documented in the [Termux guide](https://hermes-agent.nousresearch.com/docs/getting-started/termux). On Termux, Hermes installs a curated `.[termux]` extra because the full `.[all]` extra currently pulls Android-incompatible voice dependencies.\n>\n> **Windows:** Native Windows is not supported. Please install [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install) and run the command above.

After installation:

```bash\nsource ~/.bashrc    # reload shell (or: source ~/.zshrc)\nhermes              # start chatting!\n```

---

## Getting Started

```bash\nhermes              # Interactive CLI — start a conversation\nhermes model        # Choose your LLM provider and model\nhermes tools        # Configure which tools are enabled\nhermes config set   # Set individual config values\nhermes gateway      # Start the messaging gateway (Telegram, Discord, etc.)\nhermes setup        # Run the full setup wizard (configures everything at once)\nhermes claw migrate # Migrate from OpenClaw (if coming from OpenClaw)\nhermes update       # Update to the latest version\nhermes doctor       # Diagnose any issues\n```

📖 **[Full documentation →](https://hermes-agent.nousresearch.com/docs/)**

## CLI vs Messaging Quick Reference

Hermes has two entry points: start the terminal UI with `hermes`, or run the gateway and talk to it from Telegram, Discord, Slack, WhatsApp, Signal, or Email. Once you're in a conversation, many slash commands are shared across both interfaces.

| Action | CLI | Messaging platforms |\n|---------|-----|---------------------|\n| Start chatting | `hermes` | Run `hermes gateway setup` + `hermes gateway start`, then send the bot a message |\n| Start fresh conversation | `/new` or `/reset` | `/new` or `/reset` |\n| Change model | `/model [provider:model]` | `/model [provider:model]` |\n| Set a personality | `/personality [name]` | `/personality [name]` |\n| Retry or undo the last turn | `/retry`, `/undo` | `/retry`, `/undo` |\n| Compress context / check usage | `/compress`, `/usage`, `/insights [--days N]` | `/compress`, `/usage`, `/insights [days]` |\n| Browse skills | `/skills` or `/<skill-name>` | `/<skill-name>` |\n| Interrupt current work | `Ctrl+C` or send a new message | `/stop` or send a new message |\n| Platform-specific status | `/platforms` | `/status`, `/sethome` |\n\nFor the full command lists, see the [CLI guide](https://hermes-agent.nousresearch.com/docs/user-guide/cli) and the [Messaging Gateway guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging).

---

## Documentation

All documentation lives at **[hermes-agent.nousresearch.com/docs](https://hermes-agent.nousresearch.com/docs/)**:

| Section | What's Covered |\n|---------|---------------|\n| [Quickstart](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart) | Install → setup → first conversation in 2 minutes |\n| [CLI Usage](https://hermes-agent.nousresearch.com/docs/user-guide/cli) | Commands, keybindings, personalities, sessions |\n| [Configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration) | Config file, providers, models, all options |\n| [Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging) | Telegram, Discord, Slack, WhatsApp, Signal, Home Assistant |\n| [Security](https://hermes-agent.nousresearch.com/docs/user-guide/security) | Command approval, DM pairing, container isolation |\n| [Tools & Toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools) | 40+ tools, toolset system, terminal backends |\n| [Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills) | Procedural memory, Skills Hub, creating skills |\n| [Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) | Persistent memory, user profiles, best practices |\n| [MCP Integration](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp) | Connect any MCP server for extended capabilities |\n| [Cron Scheduling](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron) | Scheduled tasks with platform delivery |\n| [Context Files](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files) | Project context that shapes every conversation |\n| [Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture) | Project structure, agent loop, key classes |\n| [Contributing](https://hermes-agent.nousresearch.com/docs/developer-guide/contributing) | Development setup, PR process, code style |\n| [CLI Reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands) | All commands and flags |\n| [Environment Variables](https://hermes-agent.nousresearch.com/docs/reference/environment-variables) | Complete env var reference |\n| [Web Search](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search) | SearXNG, Firecrawl, Tavily, Exa — per-capability config |

---

## Migrating from OpenClaw

If you're coming from OpenClaw, Hermes can automatically import your settings, memories, skills, and API keys.

**During first-time setup:** The setup wizard (`hermes setup`) automatically detects `~/.openclaw` and offers to migrate before configuration begins.

**Anytime after install:**

```bash\nhermes claw migrate              # Interactive migration (full preset)\nhermes claw migrate --dry-run    # Preview what would be migrated\nhermes claw migrate --preset user-data   # Migrate without secrets\nhermes claw migrate --overwrite  # Overwrite existing conflicts\n```\n\nWhat gets imported:\n- **SOUL.md** — persona file\n- **Memories** — MEMORY.md and USER.md entries\n- **Skills** — user-created skills → `~/.hermes/skills/openclaw-imports/`\n- **Command allowlist** — approval patterns\n- **Messaging settings** — platform configs, allowed users, working directory\n- **API keys** — allowlisted secrets (Telegram, OpenRouter, OpenAI, Anthropic, ElevenLabs)\n- **TTS assets** — workspace audio files\n- **Workspace instructions** — AGENTS.md (with `--workspace-target`)

See `hermes claw migrate --help` for all options, or use the `openclaw-migration` skill for an interactive agent-guided migration with dry-run previews.

---

## Contributing

We welcome contributions! See the [Contributing Guide](https://hermes-agent.nousresearch.com/docs/developer-guide/contributing) for development setup, code style, and PR process.

Quick start for contributors — clone and go with `setup-hermes.sh`:

```bash\ngit clone https://github.com/NousResearch/hermes-agent.git\ncd hermes-agent\n./setup-hermes.sh     # installs uv, creates venv, installs .[all], symlinks ~/.local/bin/hermes\n./hermes              # auto-detects the venv, no need to `source` first\n```\n\nManual path (equivalent to the above):

```bash\ncurl -LsSf https://astral.sh/uv/install.sh | sh\nuv venv venv --python 3.11\nsource venv/bin/activate\nuv pip install -e \".[all,dev]\"\nscripts/run_tests.sh\n```\n\n> **RL Training (optional):** The RL/Atropos integration (`environments/`) ships via the `atroposlib` and `tinker` dependencies pulled in by `.[all,dev]` — no submodule setup required.

---

## Community

- 💬 [Discord](https://discord.gg/NousResearch)\n- 📚 [Skills Hub](https://agentskills.io)\n- 🐛 [Issues](https://github.com/NousResearch/hermes-agent/issues)\n- 🔌 [HermesClaw](https://github.com/AaronWong1999/hermesclaw) — Community WeChat bridge: Run Hermes Agent and OpenClaw on the same WeChat account.

---

## License

MIT — see [LICENSE](LICENSE).

Built by [Nous Research](https://nousresearch.com).
