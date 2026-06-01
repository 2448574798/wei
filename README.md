# Wei Agent

FastAPI + LangGraph service with a planner-first workflow:

- `planner` uses a low-cost model by default (`gpt-4o-mini`)
- time-sensitive requests are forced onto the research path
- research can continue into follow-up actions such as email

## Project Layout

```text
.
├─ src/
│  ├─ app.py
│  └─ tools.py
├─ deploy/
│  ├─ start_server.sh
│  ├─ gunicorn.conf.py
│  └─ wei-agent.service
├─ config/
│  └─ system_prompt.txt
├─ .env.example
├─ requirements.txt
└─ README.md
```

## Environment

Copy `.env.example` to `.env` and fill in real values:

- `ONE_API_URL`
- `ONE_API_TOKEN`
- `REDIS_URL`
- `SEARXNG_URL`
- `PLANNER_MODEL`
- `AGENT_MODEL`
- `GROUNDED_ANSWER_MODEL`
- `SMTP_*` if email sending is required

## Local Run

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
./deploy/start_server.sh
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

## API Notes

`POST /api/chat`

Optional request fields:

- `model`: main agent model
- `planner_model`: override planner model
- `grounded_model`: override grounded-answer model
- `include_tool_trace`: return tool execution summaries

## Deployment

`deploy/start_server.sh` is the single startup entrypoint for both manual runs and `systemd`.

```bash
chmod +x /opt/wei/deploy/start_server.sh
sudo cp /opt/wei/deploy/wei-agent.service /etc/systemd/system/wei-agent.service
sudo systemctl daemon-reload
sudo systemctl enable wei-agent
sudo systemctl restart wei-agent
```

## Troubleshooting

- `curl http://127.0.0.1:8000/health`
  Check `one_api_token_configured`, `smtp_configured`, and the active model names.
- `redis-cli ping`
  Verify Redis is reachable.
- `curl "http://127.0.0.1:8888/search?q=test&format=json"`
  Verify SearXNG is reachable.
- `journalctl -u wei-agent -f`
  Follow runtime logs from systemd.
