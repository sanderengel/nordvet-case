# Nordvet no-show pipeline (first slice)

A first working version of the no-show view for Nordvet's Head of Operations, built on the VetOne API.
Runs locally for the demo; the README section "Where it would run for Nordvet" describes the production setup.

> Status: skeleton. Setup and smoke tests work. Ingestion, modelling and the report are added in later steps.

## Requirements
- Python 3.10+
- Docker Desktop (on Apple Silicon: enable "Use Rosetta for x86_64/amd64 emulation")
- API credentials for the VetOne API (provided separately, not part of this repo)

## Configuration
Secrets are kept **outside** the repository, in an env file at `~/.config/nordvet/.env`.
Copy the variable names from `.env.template` and fill in the values.
To use another location, set `NORDVET_ENV_FILE`.

## Run
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e . --no-deps

docker compose --env-file ~/.config/nordvet/.env up -d    # SQL Server 2022
python -m nordvet smoke                                   # checks config, API and database
python -m nordvet run                                     # full pipeline (not implemented yet)
```

Every `docker compose` command (`up`, `ps`, `stop`, `down`) needs `--env-file ~/.config/nordvet/.env`,
because compose reads the password each time it parses `docker-compose.yml`.
Stop the database with `docker compose --env-file ~/.config/nordvet/.env stop`; the data survives in a named volume.

## Layout
```
src/nordvet/   Python package: api client, ingestion, db access, report
sql/           T-SQL by layer: raw, staging, marts, checks
tests/         unit tests (python -m pytest)
reports/       generated report for the Head of Operations
```

## Assumptions
_To be filled in as decisions are made._

## Left out on purpose
_To be filled in._

## Where it would run for Nordvet
_To be filled in._

## What I would do with more time
_To be filled in._
