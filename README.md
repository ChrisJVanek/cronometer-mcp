# Cronometer MCP

One MCP server for Cronometer that works in **Claude** (Claude Code, Claude Desktop, claude.ai web and mobile) and **ChatGPT**. It merges what every public Cronometer MCP could do (see [Credits](#credits)) into 65 tools, all tested against a live account.

Cronometer has no public API. This server uses the two private ones the official apps use:

- **The mobile API** (`mobile.cronometer.com`, plain JSON) for almost everything.
- **The web app's GWT-RPC** for what only the website does: repeating foods, macro templates and the weekly schedule, copying between any two days, deleting notes, cancelling a recurring fast, and the CSV exports.

Both log in with your Cronometer email and password. Either API can change without notice when Cronometer ships an update.

## Tools

| Area | Tools |
|---|---|
| Diary | `get_food_log`, `add_food_entry`, `edit_food_entry`, `remove_food_entry` (any entry type), `copy_day` (any day to any day), `mark_day_complete`, `find_entries_by_food`, `list_diary_groups` |
| Nutrition | `get_daily_nutrition`, `get_nutrition_scores`, `get_nutrition_summary` (day ranges with averages), `nutrient_report` (every target, lowest first), `analyze_day` (per meal and top foods), `check_food_fit` ("can I eat this?") |
| Foods | `search_foods`, `lookup_barcode`, `get_food_details`, `get_recent_foods`, `list_custom_foods`, `add_custom_food`, `update_custom_food`, `retire_custom_food`, `list_nutrients` |
| Recipes | `list_recipes`, `create_recipe`, `update_recipe`, `import_recipe` (free text, e.g. "2 tbsp ketchup") |
| Repeating foods | `get_repeated_items`, `add_repeat_item`, `delete_repeat_item` |
| Targets | `get_targets`, `set_nutrient_target`, `get_macro_targets`, `get_daily_macro_targets`, `set_macro_targets`, `list_macro_templates`, `create_macro_template`, `delete_macro_template`, `set_weekly_macro_schedule` |
| Biometrics and sleep | `list_biometrics`, `get_biometrics`, `add_biometric`, `edit_biometric`, `remove_biometric`, `log_sleep` (with stages and score) |
| Exercise | `search_activities`, `add_exercise`, `edit_exercise`, `remove_exercise` |
| Notes | `add_note`, `edit_note`, `remove_note` |
| Fasting | `get_fasting_history`, `get_fasting_stats`, `add_fast`, `edit_fast`, `delete_fast`, `cancel_active_fast` |
| Account and export | `connection_status`, `get_profile`, `get_streak`, `export_data` (servings, daily summary, exercises, biometrics, notes; JSON or CSV) |
| ChatGPT search | `search`, `fetch`: the shape ChatGPT's connectors and deep research look for |
| Paging | `get_result_page`, for results too large for one reply |

Tools are annotated read-only or destructive, so Claude and ChatGPT ask before anything that deletes.

## Three ways to use it

1. **[Connect to a hosted server](#1-connect-to-a-hosted-server)**: nothing to install. Works in Claude (web, desktop, mobile) and ChatGPT.
2. **[Run it on your own computer](#2-run-it-on-your-own-computer)**: for Claude Code and Claude Desktop.
3. **[Host your own server](#3-host-your-own-server)**: for yourself, or open to anyone.

## 1. Connect to a hosted server

A public server runs at `https://cronometer.chrisvanek.com/mcp`, or use the address of any server set up as in [part 3](#open-it-to-anyone-with-a-cronometer-account).

- **Claude** (claude.ai, Claude Desktop, mobile): Settings → Connectors → *Add custom connector*. Paste the URL, then **Connect**.
- **ChatGPT**: Plugins → Add → *Add custom MCP server*. Paste the URL and choose Authentication *OAuth*. Custom connectors depend on your ChatGPT plan.

A **Connect Cronometer** window opens. Sign in with your own Cronometer email and password, and the tools appear in your chats, working on your own diary.

Before you sign in, know that the server keeps your Cronometer password, encrypted, so it can stay signed in for you. Its operator could read it, so only use a server run by someone you trust. Accounts with two-factor login on can't connect.

## 2. Run it on your own computer

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
git clone https://github.com/ChrisJVanek/cronometer-mcp.git && cd cronometer-mcp
uv sync
cp .env.example .env    # then fill in CRONOMETER_USERNAME and CRONOMETER_PASSWORD
```

`.env` is gitignored. If your account uses two-factor login, also set `CRONOMETER_TOTP_SECRET` and run `uv sync --extra totp`.

Then add this to the `mcpServers` section of `~/.claude.json` (Claude Code) or `~/Library/Application Support/Claude/claude_desktop_config.json` (Claude Desktop):

```json
"cronometer": {
  "command": "uv",
  "args": ["run", "--frozen", "--directory", "/path/to/cronometer-mcp", "cronometer-mcp"]
}
```

If you also added a hosted connector to your Claude account, Claude Desktop already gets the tools from it; skip the local entry there so the tools are not listed twice.

## 3. Host your own server

Hosted, the server runs over HTTPS and is its own OAuth server, so Claude and ChatGPT can connect as in [part 1](#1-connect-to-a-hosted-server). It runs one of two ways:

| | Just you | Open to anyone |
|---|---|---|
| Set | `CRONOMETER_USERNAME`, `CRONOMETER_PASSWORD`, `MCP_AUTH_PASSWORD` | `MCP_SECRET_KEY` |
| The connect page asks for | the server password (`MCP_AUTH_PASSWORD`) | the person's own Cronometer email and password |
| Tools work on | your account | each person's own account |

### On Railway (or any Docker host)

The `Dockerfile` here runs the server in HTTP mode.

- **Build:** point a Railway service (or any Docker host) at this repo. The `Dockerfile` needs no build arguments.
- **Variables:** the ones in the table above, plus `MCP_PUBLIC_URL` (your public HTTPS address, e.g. `https://cronometer.example.com`). Optional: `MCP_API_KEY`, `CRONOMETER_ACCOUNT_TZ`, and `MCP_ALLOWED_HOSTS` for any other domain it should answer on (Railway's own `*.up.railway.app` name is accepted automatically).
- **Volume:** mount one at `/data`. It holds the Cronometer sessions, the OAuth clients and tokens, and in open mode the encrypted accounts, so redeploys don't disconnect anyone.
- **Health check:** `/health`.

The "approve on this computer" shortcut on the connect page only works when the server runs on your own machine.

When moving from one host to another, set `MCP_OAUTH_IMPORT` to the old `oauth.json`, base64 encoded, for one deploy. Connected apps then keep working without signing in again. Remove the variable once the log says `Imported OAuth state`.

### Open it to anyone with a Cronometer account

Set `MCP_SECRET_KEY` (16+ characters, e.g. from `openssl rand -base64 32`). Nothing about the people using it goes in the server's settings: each person adds `https://<your server>/mcp` in Claude or ChatGPT and signs in on the connect page with **their own Cronometer email and password**. The server checks them by signing in to Cronometer, and from then on every tool their app calls reads and writes their own diary.

- `CRONOMETER_USERNAME` and `CRONOMETER_PASSWORD` become optional. If set, that account signs in like anyone else, and apps connected to it before keep working.
- `MCP_ALLOWED_EMAILS` (optional) limits who may connect, e.g. `mum@example.com,dad@example.com`. Unset or `*` means anyone.
- `MCP_AUTH_PASSWORD` is not needed. `MCP_API_KEY` and the local "approve on this computer" shortcut still mean the owner account.

Good to know:

- Keep `MCP_SECRET_KEY` and never change it: it encrypts the Cronometer passwords the server keeps (in `accounts.json` on the volume) so it can sign people in again when Cronometer's session expires. A new key means everyone connects again. You, as operator, could read those passwords, so people are trusting you.
- Five wrong passwords for one email lock that email out for 15 minutes; other people are unaffected.
- Accounts with two-factor login on can't connect: the server has no way to get their code. They can turn two-factor off in Cronometer.

### On your own Mac, through a tunnel

1. Set up `.env` as in [part 2](#2-run-it-on-your-own-computer), and add `MCP_AUTH_PASSWORD` (8+ characters) or `MCP_SECRET_KEY`.
2. Point a public hostname at `http://localhost:8430`. With a Cloudflare named tunnel, add this to `~/.cloudflared/config.yml` before the catch-all rule, then run `cloudflared tunnel route dns <tunnel> cronometer.example.com`:
   ```yaml
   - hostname: cronometer.example.com
     service: http://localhost:8430
   ```
3. Run the server at login (macOS launchd, restarts if it stops):
   ```bash
   scripts/install-macos-service.sh https://cronometer.example.com
   ```
   Or run it in the foreground: `uv run cronometer-mcp --transport http --public-url https://cronometer.example.com`
4. Connect as in [part 1](#1-connect-to-a-hosted-server) with `https://cronometer.example.com/mcp`. On the Mac running the server you can also click **Approve there without a password**. That approval only works on `localhost`, so nothing coming in through the tunnel can use it.

Tokens last 24 hours and refresh for 90 days. They are kept in `~/.local/share/cronometer-mcp/oauth.json`, so a restart does not disconnect anything. Clients that can send headers can use `Authorization: Bearer $MCP_API_KEY` instead of OAuth.

Logs: `~/Library/Logs/cronometer-mcp.log`. Health check: `GET /health`.

## Development

```bash
uv run pytest          # 87 tests, offline
uv run ruff check src
```

`tests/test_oauth.py` runs the whole connector flow (discovery, registration, consent, code exchange, refresh, tools/list) without touching Cronometer.

## Notes from live testing (Nov 2026)

- `get_fasting_with_date_range` returns an empty list for every range. History now reads `/api/v2/get_fasting` and filters by date. A fast's end must be sent as `finish`; `end` is silently dropped.
- The v3 diary-entries DELETE rejects notes. Notes go through the web app's `removeNote` instead.
- The web app's repeat items label the mobile food id as `food_source_id` and the measure id as `food_id`. The tools translate both ways.
- GWT back-references (the same weekday shared by two repeat items) are resolved instead of being dropped.

## Credits

Built on [rollecode/cronometer-mcp](https://github.com/rollecode/cronometer-mcp) (MIT, itself from [rwestergren/cronometer-api-mcp](https://github.com/rwestergren/cronometer-api-mcp)) for the mobile API. The web-app client in `src/cronometer_mcp/web.py` is from [cphoskins/cronometer-mcp](https://github.com/cphoskins/cronometer-mcp) (MIT). Recipe import comes from rwestergren, sleep logging from [firaskudsy/cronometer-api-mcp](https://github.com/firaskudsy/cronometer-api-mcp), barcode and activity search from [ekelly95/cronometer-mcp](https://github.com/ekelly95/cronometer-mcp), and the "can I eat it" and day-analysis ideas from [LucasHenriqueDiniz/gocronometer-mcp](https://github.com/LucasHenriqueDiniz/gocronometer-mcp). Also reviewed: atticoos, Pop101, DiegoDeLaCruz220, DoubleAV, maxperron and DanielBrahano.

Not affiliated with Cronometer. See [DISCLAIMER.md](DISCLAIMER.md).
