# MySQL Migration Guide

This project now supports MySQL as the primary client/project configuration store.

## 1) Install dependency

```bash
pip install -r requirements.txt
```

## 2) Configure `.env`

```bash
CLIENT_STORE_BACKEND=mysql
MYSQL_URI=mysql://user:password@127.0.0.1:3306/voice_agent
# or use discrete fields:
# MYSQL_HOST=127.0.0.1
# MYSQL_PORT=3306
# MYSQL_USER=user
# MYSQL_PASSWORD=password
# MYSQL_DATABASE=voice_agent
MYSQL_CLIENTS_TABLE=clients
MYSQL_CONNECT_TIMEOUT_SECONDS=5
```

## 3) Seed MySQL from local file clients

```bash
python scripts/import_clients_to_mongo.py
```

Note: the script name is legacy, but it now writes into MySQL.

## 4) Sync projects from local files into MySQL

```bash
python scripts/sync_client_projects_to_mongo.py
```

Note: the script name is legacy, but it now writes into MySQL.

## 5) Verify

Run dashboard:

```bash
python scripts/run_dashboard.py
```

Then check:
- `GET /api/settings` returns `client_store_backend` as `mysql`
- `GET /api/clients` shows expected clients
- `GET /api/clients/{client_id}/projects` shows expected runtime settings

