# Producer example

From this directory, with the platform API running:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export WEBHOOK_PLATFORM_URL=http://localhost:8000
export WEBHOOK_PLATFORM_API_KEY='<one-time producer key>'
python app.py
```

The example uses a fixed domain operation and idempotency key so rerunning it
returns the same accepted event. Real applications should derive the key from
their own durable operation ID.
