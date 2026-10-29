---
name: littlebeaver-ceo-assistant
description: Read the local Little Beaver CEO dashboard service for its service status, API information, and latest operating brief. Use when the user asks for the CEO app's latest weekly brief or wants to check whether the local service is available. This skill is read-only and only connects to localhost.
---

# Little Beaver CEO Assistant

Read status and the latest operating brief from the Little Beaver CEO desktop app. Keep requests on loopback and treat the returned business data as private. This skill only reads data; never imply that it can import files, change settings, or alter records.

## Read the service

Use the bundled client from this skill directory:

```powershell
python scripts/ceo_client.py health
python scripts/ceo_client.py info
python scripts/ceo_client.py companies
python scripts/ceo_client.py brief
```

The app normally listens on `http://127.0.0.1:8878`. Pass `--port PORT` when it is running on another port. The client accepts only loopback hosts. If the service is unavailable, ask the user to open the CEO desktop app and retry; do not probe other machines or ports.

## Interpret responses

- `health` checks that the local service responds.
- `info` reports the service version and its read-only endpoints.
- `companies` lists local company IDs and names. Use it to identify the company before requesting a brief.
- `brief` returns the latest saved weekly briefing for company 1. Select another company with `python scripts/ceo_client.py brief --company-id ID`. A `404` means that company has no saved briefing; ask the user to generate one in the app.
- Summarize only figures and conclusions present in the response. Preserve caveats and dates, and distinguish an unavailable value from zero.

## Privacy and boundaries

- Connect only to `127.0.0.1` or `localhost` using the bundled client.
- Do not send returned company, cash-flow, customer, or supplier data to another service unless the user explicitly asks.
- Do not make up a brief when the endpoint reports that none exists.
- Do not use write endpoints or claim that this skill can modify the app.
