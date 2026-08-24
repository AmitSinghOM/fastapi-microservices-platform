# Receiver example

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export WEBHOOK_SIGNING_SECRET='<one-time endpoint signing secret>'
uvicorn app:app --port 9000
```

The receiver verifies exact raw bytes before parsing and persists each signed
event ID and body in a SQLite inbox before returning `202`. Verified duplicates
return `204`. SQLite is only an adoption example; production deployments should
use their application database and commit the unique event ID with durable
acceptance or application changes.

Platform staging/production targets require public HTTPS on port 443. Use a TLS
terminating tunnel or deployed HTTPS receiver for end-to-end platform testing;
plain local HTTP is development-only.
