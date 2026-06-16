"""Pydantic models shared across the application."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class VoiceSettings(BaseModel):
    voice_name: str = Field(default="Aoede")
    persona_gender: Literal["female", "male", "neutral"] = "female"
    speaking_rate: float = Field(default=1.0, ge=0.75, le=1.25)


class GenerationSettings(BaseModel):
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)


class ClosureExamples(BaseModel):
    positive: list[str] = Field(default_factory=list)
    negative: list[str] = Field(default_factory=list)
    non_closing_questions: list[str] = Field(default_factory=list)
    guidance: str | None = None


class ClientIdentity(BaseModel):
    display_name: str
    industry: str
    website: str | None = None
    status: Literal["active", "inactive", "draft"] = "active"
    tags: list[str] = Field(default_factory=list)


class ClientBusinessProfile(BaseModel):
    primary_offer: str
    target_audience: list[str] = Field(default_factory=list)
    tone: list[str] = Field(default_factory=list)
    languages_supported: list[Literal["marathi", "hindi", "english"]] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)


class ConversationSettings(BaseModel):
    conversation_mode: Literal["sales_discovery", "appointment_booking"] = "sales_discovery"
    default_opening_language: Literal["marathi", "hindi", "english"] | None = None
    workflow_mode: Literal["model_led", "scripted"] = "model_led"
    closure_mode: Literal["default", "appointment_booking"] = "default"
    opening_script: dict[str, str] = Field(default_factory=dict)
    closure_examples: ClosureExamples = Field(default_factory=ClosureExamples)
    disallowed_claims: list[str] = Field(default_factory=list)
    allowed_contact_fields: list[str] = Field(default_factory=lambda: ["name", "email", "phone"])
    booking_rules: dict[str, str] = Field(default_factory=dict)
    handoff_rules: dict[str, str] = Field(default_factory=dict)
    intent_overrides: dict[str, list[str]] = Field(default_factory=dict)
    results_base_template: str = "default_v1"
    results_extra_fields: list[str] = Field(default_factory=list)


class ModelRoutingSettings(BaseModel):
    live_generation: GenerationSettings = Field(
        default_factory=lambda: GenerationSettings(
            temperature=0.8,
            top_p=0.95,
            top_k=40,
            max_output_tokens=256,
        )
    )
    structured_generation: GenerationSettings = Field(
        default_factory=lambda: GenerationSettings(
            temperature=0.2,
            top_p=0.8,
            top_k=20,
            max_output_tokens=512,
        )
    )


class ImportMetadata(BaseModel):
    schema_version: int = 2
    source: str | None = None
    external_id: str | None = None
    imported_at: str | None = None
    imported_from: str | None = None
    notes: list[str] = Field(default_factory=list)


class ClientConfig(BaseModel):
    client_id: str
    identity: ClientIdentity
    business_profile: ClientBusinessProfile
    conversation: ConversationSettings = Field(default_factory=ConversationSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    model_routing: ModelRoutingSettings = Field(default_factory=ModelRoutingSettings)
    import_metadata: ImportMetadata = Field(default_factory=ImportMetadata)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_shape(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        if "identity" in data and "business_profile" in data:
            return data

        updated = dict(data)
        updated["identity"] = {
            "display_name": updated.pop("display_name", ""),
            "industry": updated.pop("industry", ""),
            "website": updated.pop("website", None),
            "status": updated.pop("status", "active"),
            "tags": updated.pop("tags", []),
        }
        raw_languages = updated.pop("languages_supported", [])
        normalized_languages: list[str] = []
        if isinstance(raw_languages, list):
            for item in raw_languages:
                value = str(item or "").strip().lower()
                if value in {"marathi", "hindi", "english"} and value not in normalized_languages:
                    normalized_languages.append(value)
        updated["business_profile"] = {
            "primary_offer": updated.pop("primary_offer", ""),
            "target_audience": updated.pop("target_audience", []),
            "tone": updated.pop("tone", []),
            "languages_supported": normalized_languages,
            "services": updated.pop("services", []),
            "locations": updated.pop("locations", []),
        }
        updated["conversation"] = {
            "conversation_mode": updated.pop("conversation_mode", "sales_discovery"),
            "default_opening_language": updated.pop("default_opening_language", None),
            "workflow_mode": updated.pop("workflow_mode", "model_led"),
            "closure_mode": updated.pop("closure_mode", "default"),
            "opening_script": updated.pop("opening_script", {}),
            "closure_examples": updated.pop("closure_examples", {}),
            "disallowed_claims": updated.pop("disallowed_claims", []),
            "allowed_contact_fields": updated.pop("allowed_contact_fields", ["name", "email", "phone"]),
            "booking_rules": updated.pop("booking_rules", {}),
            "handoff_rules": updated.pop("handoff_rules", {}),
            "intent_overrides": updated.pop("intent_overrides", {}),
            "results_base_template": updated.pop("results_base_template", "default_v1"),
            "results_extra_fields": updated.pop("results_extra_fields", []),
        }
        updated["model_routing"] = {
            "live_generation": updated.pop("live_generation", {}),
            "structured_generation": updated.pop("structured_generation", {}),
        }
        updated["import_metadata"] = updated.pop("import_metadata", {})
        return updated

    def to_editor_config(self) -> dict:
        return {
            "client_id": self.client_id,
            "display_name": self.display_name,
            "industry": self.industry,
            "website": self.website,
            "conversation_mode": self.conversation_mode,
            "default_opening_language": self.default_opening_language,
            "workflow_mode": self.workflow_mode,
            "closure_mode": self.closure_mode,
            "opening_script": self.opening_script,
            "closure_examples": self.closure_examples.model_dump(mode="json"),
            "primary_offer": self.primary_offer,
            "target_audience": self.target_audience,
            "tone": self.tone,
            "disallowed_claims": self.disallowed_claims,
            "allowed_contact_fields": self.allowed_contact_fields,
            "voice": self.voice.model_dump(mode="json"),
            "live_generation": self.live_generation.model_dump(mode="json"),
            "structured_generation": self.structured_generation.model_dump(mode="json"),
            "languages_supported": self.business_profile.languages_supported,
            "services": self.business_profile.services,
            "locations": self.business_profile.locations,
            "status": self.identity.status,
            "tags": self.identity.tags,
            "booking_rules": self.conversation.booking_rules,
            "handoff_rules": self.conversation.handoff_rules,
            "intent_overrides": self.conversation.intent_overrides,
            "results_base_template": self.conversation.results_base_template,
            "results_extra_fields": self.conversation.results_extra_fields,
            "import_metadata": self.import_metadata.model_dump(mode="json"),
        }

    @property
    def display_name(self) -> str:
        return self.identity.display_name

    @property
    def industry(self) -> str:
        return self.identity.industry

    @property
    def website(self) -> str | None:
        return self.identity.website

    @property
    def primary_offer(self) -> str:
        return self.business_profile.primary_offer

    @property
    def target_audience(self) -> list[str]:
        return self.business_profile.target_audience

    @property
    def tone(self) -> list[str]:
        return self.business_profile.tone

    @property
    def conversation_mode(self) -> Literal["sales_discovery", "appointment_booking"]:
        return self.conversation.conversation_mode

    @property
    def default_opening_language(self) -> Literal["marathi", "hindi", "english"] | None:
        return self.conversation.default_opening_language

    @property
    def workflow_mode(self) -> Literal["model_led", "scripted"]:
        return self.conversation.workflow_mode

    @property
    def closure_mode(self) -> Literal["default", "appointment_booking"]:
        return self.conversation.closure_mode

    @property
    def opening_script(self) -> dict[str, str]:
        return self.conversation.opening_script

    @property
    def closure_examples(self) -> ClosureExamples:
        return self.conversation.closure_examples

    @property
    def disallowed_claims(self) -> list[str]:
        return self.conversation.disallowed_claims

    @property
    def allowed_contact_fields(self) -> list[str]:
        return self.conversation.allowed_contact_fields

    @property
    def live_generation(self) -> GenerationSettings:
        return self.model_routing.live_generation

    @property
    def structured_generation(self) -> GenerationSettings:
        return self.model_routing.structured_generation


class ClientBundle(BaseModel):
    base_dir: Path
    config: ClientConfig
    system_prompt: str
    knowledge: str
    objections: dict
    qualification: dict
    cta: dict
    projects: list["ProjectConfig"] = Field(default_factory=list)
    active_project: "ProjectConfig | None" = None


class ProjectRuntimeConfig(BaseModel):
    gemini_api_key: str | None = None
    gemini_api_key_env: str | None = None
    live_model: str | None = None
    structured_model: str | None = None
    tts_model: str | None = None
    outreach_mode: Literal["call_only", "chat_only", "consent_then_call"] = "call_only"
    outbound_call_provider: Literal["twilio", "exotel", "airtel_iq", "meta_whatsapp", "tata", "piopiy"] | None = None
    tata_agent_number: str | None = None
    tata_caller_id: str | None = None
    piopiy_agent_id: str | None = None
    piopiy_caller_id: str | None = None
    piopiy_app_id: str | None = None
    max_concurrent_calls: int | None = Field(default=None, ge=1, le=100)
    whatsapp_consent_message: str | None = None
    whatsapp_chat_opening_message: str | None = None


class ProjectPromptAssets(BaseModel):
    system_prompt: str | None = None
    knowledge: str | None = None
    objections: dict | None = None
    qualification: dict | None = None
    cta: dict | None = None


class ProjectConfig(BaseModel):
    project_id: str
    name: str
    project_type: Literal["sales", "lead_qualification", "followup", "appointment_booking", "custom"] = "custom"
    status: Literal["active", "inactive", "draft"] = "active"
    description: str | None = None
    prompt_instruction: str | None = None
    prompt_assets: ProjectPromptAssets = Field(default_factory=ProjectPromptAssets)
    runtime: ProjectRuntimeConfig = Field(default_factory=ProjectRuntimeConfig)
    intent_overrides: dict[str, list[str]] = Field(default_factory=dict)


class TranscriptTurn(BaseModel):
    speaker: Literal["user", "agent", "system"]
    text: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    turn_id: int | None = None
    latency_ms: float | None = None


class SessionMemory(BaseModel):
    lead_name: str | None = None
    company: str | None = None
    role: str | None = None
    contact_details: dict[str, str] = Field(default_factory=dict)
    appointment_details: str | None = None
    pain_points: list[str] = Field(default_factory=list)
    use_case: str | None = None
    budget_timeline: str | None = None
    objections: list[str] = Field(default_factory=list)
    interest_level: Literal["low", "medium", "high", "unknown"] = "unknown"
    next_step: str | None = None

    def compact_context(self) -> str:
        """Return a short text block suitable for prompt augmentation or logs."""
        facts: list[str] = []
        if self.lead_name:
            facts.append(f"Lead name: {self.lead_name}")
        if self.company:
            facts.append(f"Company: {self.company}")
        if self.role:
            facts.append(f"Role: {self.role}")
        if self.contact_details:
            facts.append(f"Contact details: {self.contact_details}")
        if self.appointment_details:
            facts.append(f"Appointment details: {self.appointment_details}")
        if self.use_case:
            facts.append(f"Use case: {self.use_case}")
        if self.pain_points:
            facts.append(f"Pain points: {', '.join(self.pain_points)}")
        if self.objections:
            facts.append(f"Objections: {', '.join(self.objections)}")
        if self.budget_timeline:
            facts.append(f"Budget/timeline: {self.budget_timeline}")
        facts.append(f"Interest level: {self.interest_level}")
        if self.next_step:
            facts.append(f"Next step: {self.next_step}")
        return "\n".join(facts) if facts else "No structured lead details captured yet."


class QualificationAssessment(BaseModel):
    status: Literal["qualified", "partially_qualified", "not_qualified", "unknown"] = "unknown"
    checklist: dict[str, str] = Field(default_factory=dict)


class PostCallSummary(BaseModel):
    lead_name: str | None = None
    company: str | None = None
    role: str | None = None
    contact_details: dict[str, str] = Field(default_factory=dict)
    use_case: str | None = None
    budget_timeline_hints: str | None = None
    objections: list[str] = Field(default_factory=list)
    interest_level: Literal["low", "medium", "high", "unknown"] = "unknown"
    summary: str
    suggested_next_action: str
    qualification: QualificationAssessment = Field(default_factory=QualificationAssessment)

    @model_validator(mode="after")
    def normalize_lists(self) -> "PostCallSummary":
        self.contact_details = {
            str(key): value.strip()
            for key, value in self.contact_details.items()
            if isinstance(key, str) and isinstance(value, str) and value.strip()
        }
        self.objections = [item.strip() for item in self.objections if item.strip()]
        return self

    @model_validator(mode="before")
    @classmethod
    def normalize_contact_details(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        raw_contact_details = data.get("contact_details")
        if not isinstance(raw_contact_details, dict):
            return data

        cleaned = {
            str(key): value
            for key, value in raw_contact_details.items()
            if value is not None
        }
        updated = dict(data)
        updated["contact_details"] = cleaned
        return updated


class CriticalCallDetails(BaseModel):
    name: str | None = None
    age: int | None = None
    appointment_date: str | None = None
    appointment_time: str | None = None
    important_questions_asked: list[str] = Field(default_factory=list)


class CostLineItem(BaseModel):
    provider: str
    category: str
    unit: str
    quantity: float = 0.0
    rate: float = 0.0
    currency: str = "INR"
    estimated_cost: float = 0.0
    metadata: dict[str, str | float | int | bool | None] = Field(default_factory=dict)


class ActualCostLedger(BaseModel):
    status: Literal["estimated", "partial", "finalized"] = "estimated"
    currency: str = "INR"
    telephony: list[CostLineItem] = Field(default_factory=list)
    gemini: list[CostLineItem] = Field(default_factory=list)
    total_estimated_cost: float = 0.0
    notes: list[str] = Field(default_factory=list)
    provider_call_sid: str | None = None
    telephony_provider: str | None = None
    raw_provider_usage: dict[str, str | float | int | bool | None] = Field(default_factory=dict)
    raw_model_usage: dict[str, str | float | int | bool | None] = Field(default_factory=dict)


class SessionArtifacts(BaseModel):
    client_id: str
    project_id: str | None = None
    project_name: str | None = None
    session_id: str
    started_at: datetime
    ended_at: datetime | None = None
    transcript: list[TranscriptTurn] = Field(default_factory=list)
    parallel_stt_transcript: str | None = None
    parallel_stt_segments: list[str] = Field(default_factory=list)
    recording_stt_corpus: str | None = None
    recording_stt_full_corpus: str | None = None
    recording_llm_details: CriticalCallDetails | None = None
    piopiy_recording_url: str | None = None
    piopiy_recording_path: str | None = None
    piopiy_recording_filename: str | None = None
    piopiy_recording_content_type: str | None = None
    piopiy_recording_downloaded_at: datetime | None = None
    piopiy_recording_size_bytes: int | None = None
    telephony_context: dict[str, Any] | None = None
    memory: SessionMemory = Field(default_factory=SessionMemory)
    summary: PostCallSummary | None = None
    errors: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    actual_cost: ActualCostLedger | None = None
