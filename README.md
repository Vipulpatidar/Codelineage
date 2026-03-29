# CodeLineage 🔍

An AI-powered GitHub PR reviewer that automatically analyzes the cross-file blast radius of code changes and posts structured reviews with exact crash point attribution — going far beyond what standard diff-level tools can see.

## How It Works

When a pull request is opened or updated, CodeLineage:

1. **Receives a GitHub webhook** and fetches the PR diff
2. **Builds a repo-wide knowledge graph** by parsing every Python file via a custom AST engine — tracking function call chains, caller-callee relationships, cross-file dependencies, exports, and import links
3. **Runs a 3-node LangGraph pipeline:**
   - `impact_analyzer` — identifies which functions actually changed (diff-aware, line-level) and traces execution paths from entry points down to changed functions
   - `context_builder` — assembles the full LLM context: git diff, changed function sources, impacted callers, DB models, API routes, and call chains
   - `critic` — calls **Gemini 2.5 Flash** to generate a structured review with exact file + line crash attribution; retries with a self-correction pass if confidence is low
4. **Posts the review as a comment** directly on the GitHub PR

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/Vipulpatidar/Codelineage.git
cd Codelineage
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create a `.env` file

Create a `.env` file in the root of the project with the following keys:

```properties
GITHUB_TOKEN=
GEMINI_API_KEY=
GITHUB_WEBHOOK_SECRET=
```

- **GITHUB_TOKEN** — a GitHub Personal Access Token with `repo` scope. Generate one at [github.com/settings/tokens](https://github.com/settings/tokens)
- **GEMINI_API_KEY** — your Gemini API key from [aistudio.google.com](https://aistudio.google.com)
- **GITHUB_WEBHOOK_SECRET** — a secret string of your choice; you'll use the same value when configuring the webhook in GitHub

### 4. Run the server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

The server will start at `http://localhost:8000`. You can verify it's running by visiting that URL — you should see:

```json
{"status": "CodeLineage is running", "version": "0.2.0"}
```

---

## Setting Up the GitHub Webhook

For GitHub to send PR events to your server, you need to expose it publicly. Use [ngrok](https://ngrok.com) for local development:

```bash
ngrok http 8000
```

Copy the `https://` forwarding URL (e.g. `https://abc123.ngrok.io`).

Then in your GitHub repository:

1. Go to **Settings → Webhooks → Add webhook**
2. Set **Payload URL** to `https://abc123.ngrok.io/webhook`
3. Set **Content type** to `application/json`
4. Set **Secret** to the same value as `GITHUB_WEBHOOK_SECRET` in your `.env`
5. Under **Which events**, select **Let me select individual events** and check **Pull requests**
6. Click **Add webhook**

GitHub will send a ping event to verify the connection. You should see a `✓` next to the webhook if it's working.

---

## Project Structure

```
app/
├── main.py              # FastAPI entry point, webhook handler
├── agents/
│   ├── impact_analyzer.py   # Diff-aware blast radius analysis + path tracing
│   ├── context_builder.py   # Assembles LLM prompt context from the graph
│   ├── critic.py            # Gemini reviewer with self-correction loop
│   └── pipeline.py          # LangGraph StateGraph wiring
├── tools/
│   ├── ast_tool.py          # Python AST parser — builds the knowledge graph
│   └── github_tool.py       # GitHub API client, graph indexer, comment poster
└── utils/
    ├── graph.py             # In-memory knowledge graph cache
    ├── graph_state.py       # LangGraph shared state (TypedDict)
    └── graph_context.py     # Dependency-aware context collector
```

---

## Requirements

- Python 3.11+
- A GitHub repository you have admin access to (for webhook setup)
- A Gemini API key
- A GitHub Personal Access Token with `repo` scope
