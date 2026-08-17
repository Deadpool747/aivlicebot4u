"""Client asset loading and validation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .config import load_settings
from .constants import CLIENTS_DIR
from .models import ClientBundle, ClientConfig, ProjectConfig


REQUIRED_FILES = {
    "config": "config.json",
    "system_prompt": "system_prompt.txt",
    "knowledge": "knowledge.md",
    "objections": "objections.json",
    "qualification": "qualification.json",
    "cta": "cta.json",
}
PROJECTS_FILE = "projects.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_bundle_from_payload(client_id: str, payload: dict[str, Any], base_dir: Path) -> ClientBundle:
    config_payload = dict(payload.get("config") or {})
    config_payload["client_id"] = client_id
    projects = _load_projects_from_payload(config_payload, payload.get("projects"))
    return ClientBundle(
        base_dir=base_dir / client_id,
        config=ClientConfig.model_validate(config_payload),
        system_prompt=str(payload.get("system_prompt", "")).strip(),
        knowledge=str(payload.get("knowledge", "")).strip(),
        objections=payload.get("objections") or {},
        qualification=payload.get("qualification") or {},
        cta=payload.get("cta") or {},
        projects=projects,
        active_project=_select_project(projects, payload.get("project_id")),
    )


def _default_project_payload(config_payload: dict[str, Any]) -> dict[str, Any]:
    conversation = config_payload.get("conversation") or {}
    return {
        "project_id": "default",
        "name": f"{(config_payload.get('identity') or {}).get('display_name', config_payload.get('client_id', 'Client'))} Default Agent",
        "project_type": conversation.get("conversation_mode") or "custom",
        "status": "active",
        "description": "Fallback project generated from the client config.",
        "prompt_instruction": None,
        "runtime": {},
    }


def _load_projects_from_payload(config_payload: dict[str, Any], payload: Any) -> list[ProjectConfig]:
    raw_projects: list[dict[str, Any]]
    if isinstance(payload, dict):
        raw_projects = list(payload.get("projects") or [])
    elif isinstance(payload, list):
        raw_projects = list(payload)
    else:
        raw_projects = []
    if not raw_projects:
        raw_projects = [_default_project_payload(config_payload)]
    normalized_projects: list[dict[str, Any]] = []
    for project in raw_projects:
        normalized = dict(project)
        project_type = str(normalized.get("project_type") or "").strip().lower()
        if project_type == "sales_discovery":
            normalized["project_type"] = "sales"
        elif project_type in {"qualification", "lead_qualification_agent"}:
            normalized["project_type"] = "lead_qualification"
        elif project_type in {"follow_up", "follow_up_agent"}:
            normalized["project_type"] = "followup"
        elif project_type not in {"sales", "lead_qualification", "followup", "appointment_booking", "custom"}:
            normalized["project_type"] = "custom"
        normalized_projects.append(normalized)
    return [ProjectConfig.model_validate(project) for project in normalized_projects]


def _select_project(projects: list[ProjectConfig], requested_project_id: str | None = None) -> ProjectConfig | None:
    if not projects:
        return None
    if requested_project_id:
        for project in projects:
            if project.project_id == requested_project_id:
                return project
    for project in projects:
        if project.status == "active":
            return project
    return projects[0]


def _editor_project_payload(project: ProjectConfig) -> dict[str, Any]:
    return {
        "project_id": project.project_id,
        "name": project.name,
        "project_type": project.project_type,
        "status": project.status,
        "description": project.description,
        "prompt_instruction": project.prompt_instruction,
        "prompt_assets": {
            "system_prompt": project.prompt_assets.system_prompt,
            "knowledge": project.prompt_assets.knowledge,
            "objections": project.prompt_assets.objections,
            "qualification": project.prompt_assets.qualification,
            "cta": project.prompt_assets.cta,
        },
        "intent_overrides": project.intent_overrides,
        "runtime": {
            "gemini_api_key_present": bool(project.runtime.gemini_api_key),
            "gemini_api_key_env": project.runtime.gemini_api_key_env,
            "live_model": project.runtime.live_model,
            "structured_model": project.runtime.structured_model,
            "tts_model": project.runtime.tts_model,
            "outreach_mode": project.runtime.outreach_mode,
            "outbound_call_provider": project.runtime.outbound_call_provider,
            "piopiy_agent_id": project.runtime.piopiy_agent_id,
            "piopiy_caller_id": project.runtime.piopiy_caller_id,
            "piopiy_dids": project.runtime.piopiy_dids,
            "piopiy_app_id": project.runtime.piopiy_app_id,
            "whatsapp_consent_message": project.runtime.whatsapp_consent_message,
            "whatsapp_chat_opening_message": project.runtime.whatsapp_chat_opening_message,
        },
    }


def _projects_payload_for_save(
    config_payload: dict[str, Any],
    incoming_payload: Any,
    existing_projects: list[ProjectConfig] | None = None,
) -> list[dict[str, Any]]:
    existing_by_id = {project.project_id: project for project in (existing_projects or [])}
    raw_projects: list[dict[str, Any]]
    if isinstance(incoming_payload, dict):
        raw_projects = list(incoming_payload.get("projects") or [])
    elif isinstance(incoming_payload, list):
        raw_projects = list(incoming_payload)
    else:
        raw_projects = []
    if not raw_projects:
        raw_projects = [_default_project_payload(config_payload)]

    merged_payloads: list[dict[str, Any]] = []
    for project in raw_projects:
        normalized = dict(project)
        runtime = dict(normalized.get("runtime") or {})
        existing = existing_by_id.get(str(normalized.get("project_id") or ""))
        if not runtime.get("gemini_api_key") and existing and existing.runtime.gemini_api_key:
            runtime["gemini_api_key"] = existing.runtime.gemini_api_key
        if "piopiy_dids" not in runtime and existing:
            runtime["piopiy_dids"] = existing.runtime.piopiy_dids
        normalized["runtime"] = runtime
        merged_payloads.append(normalized)

    return [project.model_dump(mode="json") for project in _load_projects_from_payload(config_payload, merged_payloads)]


def _active_project_id_from_payload(
    projects_payload: list[dict[str, Any]],
    requested_active_project_id: str | None = None,
) -> str | None:
    requested = str(requested_active_project_id or "").strip()
    if requested:
        for project in projects_payload:
            if str(project.get("project_id") or "").strip() == requested:
                return requested
    for project in projects_payload:
        if str(project.get("status") or "").strip().lower() == "active":
            project_id = str(project.get("project_id") or "").strip()
            if project_id:
                return project_id
    if projects_payload:
        fallback = str(projects_payload[0].get("project_id") or "").strip()
        if fallback:
            return fallback
    return None


def _resolve_effective_prompt_assets(
    *,
    fallback_system_prompt: str,
    fallback_knowledge: str,
    fallback_objections: dict[str, Any],
    fallback_qualification: dict[str, Any],
    fallback_cta: dict[str, Any],
    projects_payload: list[dict[str, Any]],
    active_project_id: str | None,
) -> dict[str, Any]:
    active_id = str(active_project_id or "").strip()
    if not active_id:
        return {
            "system_prompt": fallback_system_prompt,
            "knowledge": fallback_knowledge,
            "objections": fallback_objections,
            "qualification": fallback_qualification,
            "cta": fallback_cta,
        }
    for project in projects_payload:
        if str(project.get("project_id") or "").strip() != active_id:
            continue
        prompt_assets = project.get("prompt_assets")
        if not isinstance(prompt_assets, dict):
            break
        return {
            "system_prompt": str(prompt_assets.get("system_prompt") or fallback_system_prompt).strip(),
            "knowledge": str(prompt_assets.get("knowledge") or fallback_knowledge).strip(),
            "objections": (
                prompt_assets.get("objections")
                if isinstance(prompt_assets.get("objections"), dict)
                else fallback_objections
            ),
            "qualification": (
                prompt_assets.get("qualification")
                if isinstance(prompt_assets.get("qualification"), dict)
                else fallback_qualification
            ),
            "cta": (
                prompt_assets.get("cta")
                if isinstance(prompt_assets.get("cta"), dict)
                else fallback_cta
            ),
        }
    return {
        "system_prompt": fallback_system_prompt,
        "knowledge": fallback_knowledge,
        "objections": fallback_objections,
        "qualification": fallback_qualification,
        "cta": fallback_cta,
    }


class FileClientRepository:
    """Load and save clients from local folders."""

    def __init__(self, base_dir: Path = CLIENTS_DIR) -> None:
        self.base_dir = base_dir

    def list_client_ids(self) -> list[str]:
        if not self.base_dir.exists():
            return []
        return sorted(item.name for item in self.base_dir.iterdir() if item.is_dir())

    def load_client(self, client_id: str, project_id: str | None = None) -> ClientBundle:
        client_dir = self.base_dir / client_id
        if not client_dir.exists():
            raise FileNotFoundError(f"Client '{client_id}' does not exist under {self.base_dir}.")

        missing = [name for name, filename in REQUIRED_FILES.items() if not (client_dir / filename).exists()]
        if missing:
            raise FileNotFoundError(f"Client '{client_id}' is missing required files: {', '.join(missing)}")

        with (client_dir / REQUIRED_FILES["config"]).open("r", encoding="utf-8") as handle:
            config = ClientConfig.model_validate(json.load(handle))

        with (client_dir / REQUIRED_FILES["system_prompt"]).open("r", encoding="utf-8") as handle:
            system_prompt = handle.read().strip()

        with (client_dir / REQUIRED_FILES["knowledge"]).open("r", encoding="utf-8") as handle:
            knowledge = handle.read().strip()

        with (client_dir / REQUIRED_FILES["objections"]).open("r", encoding="utf-8") as handle:
            objections = json.load(handle)

        with (client_dir / REQUIRED_FILES["qualification"]).open("r", encoding="utf-8") as handle:
            qualification = json.load(handle)

        with (client_dir / REQUIRED_FILES["cta"]).open("r", encoding="utf-8") as handle:
            cta = json.load(handle)

        projects_payload: Any = []
        projects_path = client_dir / PROJECTS_FILE
        if projects_path.exists():
            with projects_path.open("r", encoding="utf-8") as handle:
                projects_payload = json.load(handle)

        projects = _load_projects_from_payload(config.model_dump(mode="python"), projects_payload)
        return ClientBundle(
            base_dir=client_dir,
            config=config,
            system_prompt=system_prompt,
            knowledge=knowledge,
            objections=objections,
            qualification=qualification,
            cta=cta,
            projects=projects,
            active_project=_select_project(projects, project_id),
        )

    def get_editor_payload(self, client_id: str) -> dict[str, Any]:
        bundle = self.load_client(client_id)
        return {
            "client_id": client_id,
            "config": bundle.config.to_editor_config(),
            "system_prompt": bundle.system_prompt,
            "knowledge": bundle.knowledge,
            "objections": bundle.objections,
            "qualification": bundle.qualification,
            "cta": bundle.cta,
            "projects": [_editor_project_payload(project) for project in bundle.projects],
            "active_project_id": bundle.active_project.project_id if bundle.active_project else None,
        }

    def save_editor_payload(self, client_id: str, payload: dict[str, Any]) -> None:
        client_dir = self.base_dir / client_id
        if not client_dir.exists():
            raise FileNotFoundError(f"Client '{client_id}' does not exist under {self.base_dir}.")

        config_payload = dict(payload["config"])
        config_payload["client_id"] = client_id
        config = ClientConfig.model_validate(config_payload)

        text_assets = {
            "system_prompt": str(payload["system_prompt"]).strip(),
            "knowledge": str(payload["knowledge"]).strip(),
        }
        for key, value in text_assets.items():
            if not value:
                raise ValueError(f"{key} must not be empty.")

        json_assets = {
            "objections": payload["objections"],
            "qualification": payload["qualification"],
            "cta": payload["cta"],
        }
        try:
            existing_projects = self.load_client(client_id).projects
        except FileNotFoundError:
            existing_projects = []
        projects_payload = payload.get("projects")
        validated_projects: list[dict[str, Any]] = []
        if projects_payload is not None:
            validated_projects = _projects_payload_for_save(
                config.model_dump(mode="python"),
                projects_payload,
                existing_projects=existing_projects,
            )
            requested_active_project_id = str(payload.get("active_project_id") or "").strip() or None
            active_project_id = _active_project_id_from_payload(
                validated_projects,
                requested_active_project_id=requested_active_project_id,
            )
            effective_assets = _resolve_effective_prompt_assets(
                fallback_system_prompt=text_assets["system_prompt"],
                fallback_knowledge=text_assets["knowledge"],
                fallback_objections=json_assets["objections"],
                fallback_qualification=json_assets["qualification"],
                fallback_cta=json_assets["cta"],
                projects_payload=validated_projects,
                active_project_id=active_project_id,
            )
            text_assets["system_prompt"] = str(effective_assets["system_prompt"]).strip()
            text_assets["knowledge"] = str(effective_assets["knowledge"]).strip()
            json_assets["objections"] = effective_assets["objections"]
            json_assets["qualification"] = effective_assets["qualification"]
            json_assets["cta"] = effective_assets["cta"]

        (client_dir / REQUIRED_FILES["config"]).write_text(
            json.dumps(config.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        (client_dir / REQUIRED_FILES["system_prompt"]).write_text(text_assets["system_prompt"] + "\n", encoding="utf-8")
        (client_dir / REQUIRED_FILES["knowledge"]).write_text(text_assets["knowledge"] + "\n", encoding="utf-8")
        for key, value in json_assets.items():
            (client_dir / REQUIRED_FILES[key]).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        if projects_payload is not None:
            (client_dir / PROJECTS_FILE).write_text(
                json.dumps(validated_projects, indent=2) + "\n",
                encoding="utf-8",
            )

        issues = validate_client_bundle(client_id, repository=self)
        if issues:
            raise ValueError("; ".join(issues))


class MySQLClientRepository:
    """Load and save client bundles from MySQL."""

    def __init__(
        self,
        mysql_uri: str | None,
        host: str | None,
        port: int,
        user: str | None,
        password: str | None,
        database_name: str,
        table_name: str,
        connect_timeout_seconds: int = 5,
        base_dir: Path = CLIENTS_DIR,
    ) -> None:
        try:
            import pymysql
            import pymysql.cursors
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "MySQL support requires PyMySQL. Add it to requirements and install dependencies."
            ) from exc

        self._pymysql = pymysql
        self._DictCursor = pymysql.cursors.DictCursor
        self.base_dir = base_dir
        resolved = self._resolve_connection_settings(
            mysql_uri=mysql_uri,
            host=host,
            port=port,
            user=user,
            password=password,
            database_name=database_name,
        )
        self._connection_kwargs = {
            "host": resolved["host"],
            "port": resolved["port"],
            "user": resolved["user"],
            "password": resolved["password"],
            "database": resolved["database"],
            "connect_timeout": connect_timeout_seconds,
            "charset": "utf8mb4",
            "autocommit": True,
            "cursorclass": self._DictCursor,
        }
        self._table_name = table_name
        self._ensure_schema()

    @staticmethod
    def _resolve_connection_settings(
        *,
        mysql_uri: str | None,
        host: str | None,
        port: int,
        user: str | None,
        password: str | None,
        database_name: str,
    ) -> dict[str, Any]:
        if mysql_uri:
            parsed = urlparse(mysql_uri)
            if parsed.scheme not in {"mysql", "mysql+pymysql"}:
                raise RuntimeError("MYSQL_URI must start with mysql:// or mysql+pymysql://")
            query = parse_qs(parsed.query or "")
            query_port = query.get("port", [None])[0]
            resolved_port = int(query_port) if query_port else (parsed.port or port or 3306)
            resolved_database = (parsed.path or "").lstrip("/") or database_name
            return {
                "host": parsed.hostname or host or "127.0.0.1",
                "port": resolved_port,
                "user": unquote(parsed.username or "") or user,
                "password": unquote(parsed.password or "") if parsed.password is not None else password,
                "database": resolved_database,
            }
        return {
            "host": host or "127.0.0.1",
            "port": port or 3306,
            "user": user,
            "password": password,
            "database": database_name,
        }

    def _connect(self):
        return self._pymysql.connect(**self._connection_kwargs)

    def _ensure_schema(self) -> None:
        ddl = f"""
        CREATE TABLE IF NOT EXISTS `{self._table_name}` (
            `client_id` VARCHAR(191) NOT NULL,
            `config_json` LONGTEXT NOT NULL,
            `system_prompt` LONGTEXT NOT NULL,
            `knowledge` LONGTEXT NOT NULL,
            `objections_json` LONGTEXT NOT NULL,
            `qualification_json` LONGTEXT NOT NULL,
            `cta_json` LONGTEXT NOT NULL,
            `projects_json` LONGTEXT NULL,
            `import_metadata_json` LONGTEXT NULL,
            `created_at` DATETIME(6) NOT NULL,
            `updated_at` DATETIME(6) NOT NULL,
            PRIMARY KEY (`client_id`)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
        """
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(ddl)

    def _load_payload(self, client_id: str) -> dict[str, Any]:
        select_sql = f"""
        SELECT
            client_id,
            config_json,
            system_prompt,
            knowledge,
            objections_json,
            qualification_json,
            cta_json,
            projects_json,
            import_metadata_json
        FROM `{self._table_name}`
        WHERE client_id = %s
        LIMIT 1
        """
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(select_sql, (client_id,))
                row = cursor.fetchone()
        if row is None:
            raise FileNotFoundError(f"Client '{client_id}' does not exist in MySQL.")
        return {
            "client_id": str(row.get("client_id") or ""),
            "config": json.loads(str(row.get("config_json") or "{}")),
            "system_prompt": str(row.get("system_prompt") or ""),
            "knowledge": str(row.get("knowledge") or ""),
            "objections": json.loads(str(row.get("objections_json") or "{}")),
            "qualification": json.loads(str(row.get("qualification_json") or "{}")),
            "cta": json.loads(str(row.get("cta_json") or "{}")),
            "projects": json.loads(str(row.get("projects_json") or "[]")),
            "import_metadata": json.loads(str(row.get("import_metadata_json") or "{}")),
        }

    def list_client_ids(self) -> list[str]:
        sql = f"SELECT client_id FROM `{self._table_name}` ORDER BY client_id ASC"
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                rows = cursor.fetchall() or []
        return [str(item["client_id"]) for item in rows if item.get("client_id")]

    def load_client(self, client_id: str, project_id: str | None = None) -> ClientBundle:
        payload = self._load_payload(client_id)
        payload["project_id"] = project_id
        return _build_bundle_from_payload(client_id, payload, self.base_dir)

    def get_editor_payload(self, client_id: str) -> dict[str, Any]:
        bundle = self.load_client(client_id)
        return {
            "client_id": client_id,
            "config": bundle.config.to_editor_config(),
            "system_prompt": bundle.system_prompt,
            "knowledge": bundle.knowledge,
            "objections": bundle.objections,
            "qualification": bundle.qualification,
            "cta": bundle.cta,
            "import_metadata": bundle.config.import_metadata.model_dump(mode="json"),
            "projects": [_editor_project_payload(project) for project in bundle.projects],
            "active_project_id": bundle.active_project.project_id if bundle.active_project else None,
        }

    def save_editor_payload(self, client_id: str, payload: dict[str, Any]) -> None:
        config_payload = dict(payload["config"])
        config_payload["client_id"] = client_id
        config = ClientConfig.model_validate(config_payload)
        existing_projects: list[ProjectConfig] = []
        try:
            existing_projects = self.load_client(client_id).projects
        except FileNotFoundError:
            existing_projects = []

        text_system_prompt = str(payload["system_prompt"]).strip()
        text_knowledge = str(payload["knowledge"]).strip()
        json_objections = payload["objections"]
        json_qualification = payload["qualification"]
        json_cta = payload["cta"]
        validated_projects = _projects_payload_for_save(
            config.model_dump(mode="python"),
            payload.get("projects"),
            existing_projects=existing_projects,
        )
        requested_active_project_id = str(payload.get("active_project_id") or "").strip() or None
        active_project_id = _active_project_id_from_payload(
            validated_projects,
            requested_active_project_id=requested_active_project_id,
        )
        effective_assets = _resolve_effective_prompt_assets(
            fallback_system_prompt=text_system_prompt,
            fallback_knowledge=text_knowledge,
            fallback_objections=json_objections,
            fallback_qualification=json_qualification,
            fallback_cta=json_cta,
            projects_payload=validated_projects,
            active_project_id=active_project_id,
        )

        document = {
            "client_id": client_id,
            "config": config.model_dump(mode="json"),
            "system_prompt": str(effective_assets["system_prompt"]).strip(),
            "knowledge": str(effective_assets["knowledge"]).strip(),
            "objections": effective_assets["objections"],
            "qualification": effective_assets["qualification"],
            "cta": effective_assets["cta"],
            "projects": validated_projects,
            "import_metadata": payload.get("import_metadata") or {},
            "updated_at": _utc_now_iso(),
        }
        if not document["system_prompt"]:
            raise ValueError("system_prompt must not be empty.")
        if not document["knowledge"]:
            raise ValueError("knowledge must not be empty.")

        upsert_sql = f"""
        INSERT INTO `{self._table_name}` (
            client_id,
            config_json,
            system_prompt,
            knowledge,
            objections_json,
            qualification_json,
            cta_json,
            projects_json,
            import_metadata_json,
            created_at,
            updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(6), NOW(6))
        ON DUPLICATE KEY UPDATE
            config_json = VALUES(config_json),
            system_prompt = VALUES(system_prompt),
            knowledge = VALUES(knowledge),
            objections_json = VALUES(objections_json),
            qualification_json = VALUES(qualification_json),
            cta_json = VALUES(cta_json),
            projects_json = VALUES(projects_json),
            import_metadata_json = VALUES(import_metadata_json),
            updated_at = NOW(6)
        """
        params = (
            client_id,
            json.dumps(document["config"], ensure_ascii=True),
            document["system_prompt"],
            document["knowledge"],
            json.dumps(document["objections"], ensure_ascii=True),
            json.dumps(document["qualification"], ensure_ascii=True),
            json.dumps(document["cta"], ensure_ascii=True),
            json.dumps(document["projects"], ensure_ascii=True),
            json.dumps(document["import_metadata"], ensure_ascii=True),
        )
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(upsert_sql, params)
        issues = validate_client_bundle(client_id, repository=self)
        if issues:
            raise ValueError("; ".join(issues))


def _build_repository(backend: str | None = None):
    settings = load_settings()
    selected = backend or settings.client_store_backend
    if selected not in {"auto", "file", "mysql", "mongo"}:
        raise RuntimeError(f"Unsupported client storage backend: {selected}")

    if selected == "file":
        return FileClientRepository()

    if selected in {"mysql", "mongo"}:
        if not settings.mysql_uri and (not settings.mysql_host or not settings.mysql_user):
            if selected == "mongo":
                # Backward-compatible fallback while environments migrate from MongoDB settings.
                return FileClientRepository()
            raise RuntimeError(
                "CLIENT_STORE_BACKEND is set to MySQL but MYSQL_URI (or MYSQL_HOST + MYSQL_USER) is missing."
            )
        return MySQLClientRepository(
            mysql_uri=settings.mysql_uri,
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            database_name=settings.mysql_database,
            table_name=settings.mysql_clients_table,
            connect_timeout_seconds=settings.mysql_connect_timeout_seconds,
        )

    if settings.mysql_uri or settings.mysql_host:
        return MySQLClientRepository(
            mysql_uri=settings.mysql_uri,
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            database_name=settings.mysql_database,
            table_name=settings.mysql_clients_table,
            connect_timeout_seconds=settings.mysql_connect_timeout_seconds,
        )
    return FileClientRepository()


def list_client_ids(base_dir: Path = CLIENTS_DIR, backend: str | None = None) -> list[str]:
    """Return all client IDs from the active repository."""
    repository = _build_repository(backend=backend)
    if isinstance(repository, FileClientRepository):
        repository.base_dir = base_dir
    return repository.list_client_ids()


def list_client_projects(
    client_id: str,
    base_dir: Path = CLIENTS_DIR,
    backend: str | None = None,
) -> list[dict[str, Any]]:
    """Return safe project metadata for a client."""
    bundle = load_client(client_id, base_dir=base_dir, backend=backend)
    active_project_id = bundle.active_project.project_id if bundle.active_project else None
    return [
        {
            "project_id": project.project_id,
            "name": project.name,
            "project_type": project.project_type,
            "status": project.status,
            "description": project.description,
            "runtime": {
                "gemini_api_key_present": bool(project.runtime.gemini_api_key),
                "gemini_api_key_env": project.runtime.gemini_api_key_env,
                "live_model": project.runtime.live_model,
                "structured_model": project.runtime.structured_model,
                "tts_model": project.runtime.tts_model,
                "outreach_mode": project.runtime.outreach_mode,
                "outbound_call_provider": project.runtime.outbound_call_provider,
                "piopiy_agent_id": project.runtime.piopiy_agent_id,
                "piopiy_caller_id": project.runtime.piopiy_caller_id,
                "piopiy_app_id": project.runtime.piopiy_app_id,
                "whatsapp_consent_message": project.runtime.whatsapp_consent_message,
                "whatsapp_chat_opening_message": project.runtime.whatsapp_chat_opening_message,
            },
            "is_active": project.project_id == active_project_id,
        }
        for project in bundle.projects
    ]


def load_client(
    client_id: str,
    project_id: str | None = None,
    base_dir: Path = CLIENTS_DIR,
    backend: str | None = None,
) -> ClientBundle:
    """Load a client bundle from the active repository."""
    repository = _build_repository(backend=backend)
    if isinstance(repository, FileClientRepository):
        repository.base_dir = base_dir
    return repository.load_client(client_id, project_id=project_id)


def validate_client_bundle(
    client_id: str,
    base_dir: Path = CLIENTS_DIR,
    repository=None,
    backend: str | None = None,
) -> list[str]:
    """Validate client assets and return a list of human-readable issues."""
    issues: list[str] = []
    repository = repository or _build_repository(backend=backend)

    if isinstance(repository, FileClientRepository):
        client_dir = base_dir / client_id
        if not client_dir.exists():
            return [f"Missing client directory: {client_dir}"]
        for filename in REQUIRED_FILES.values():
            if not (client_dir / filename).exists():
                issues.append(f"Missing required file: {client_dir / filename}")
        if issues:
            return issues

    try:
        bundle = repository.load_client(client_id)
    except Exception as exc:  # pragma: no cover - defensive validation path
        return [f"Failed to load client '{client_id}': {exc}"]

    if bundle.config.client_id != client_id:
        issues.append(
            f"config client_id '{bundle.config.client_id}' does not match requested client '{client_id}'"
        )
    if not bundle.knowledge:
        issues.append("knowledge must not be empty")
    if not bundle.system_prompt:
        issues.append("system_prompt must not be empty")
    if "common" not in bundle.objections:
        issues.append("objections should include a 'common' objection list")
    if "fields" not in bundle.qualification:
        issues.append("qualification should include a 'fields' object")
    if "primary_cta" not in bundle.cta:
        issues.append("cta should include a 'primary_cta' field")

    return issues


def get_client_editor_payload(
    client_id: str,
    base_dir: Path = CLIENTS_DIR,
    backend: str | None = None,
) -> dict[str, Any]:
    """Return editable client assets for the dashboard editor."""
    repository = _build_repository(backend=backend)
    if isinstance(repository, FileClientRepository):
        repository.base_dir = base_dir
    return repository.get_editor_payload(client_id)


def save_client_editor_payload(
    client_id: str,
    payload: dict[str, Any],
    base_dir: Path = CLIENTS_DIR,
    backend: str | None = None,
) -> None:
    """Persist editable client assets in the active repository."""
    repository = _build_repository(backend=backend)
    if isinstance(repository, FileClientRepository):
        repository.base_dir = base_dir
    repository.save_editor_payload(client_id, payload)


def export_file_client_payload(client_id: str, base_dir: Path = CLIENTS_DIR) -> dict[str, Any]:
    """Read one local folder client and return a DB-friendly payload."""
    repository = FileClientRepository(base_dir=base_dir)
    payload = repository.get_editor_payload(client_id)
    payload["import_metadata"] = {
        "source": "local_files",
        "imported_from": str((base_dir / client_id).resolve()),
        "imported_at": _utc_now_iso(),
        "schema_version": 1,
    }
    return payload
