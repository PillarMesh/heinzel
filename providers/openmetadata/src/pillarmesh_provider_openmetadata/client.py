from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Annotated, Literal, Protocol
from uuid import UUID

import httpx
from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_provider_sdk import (
    CatalogNativeTableDefinition,
    CatalogNativeTableObservation,
    CatalogProductDefinition,
    CatalogProductObservation,
)
from pydantic import (
    AnyUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    ValidationError,
)

from .models import (
    CatalogFailureClassification,
    CatalogObjectRef,
    CatalogObjectSnapshot,
    CatalogProviderError,
    ClassificationPayload,
    GlossaryTermPayload,
    LineagePayload,
    ProviderBuildIdentity,
    ProviderHealth,
)

type CatalogObjectKind = Literal["namespace", "glossary_term", "classification", "lineage"]

_METADATA_MARKER = "\n\nPillarMesh metadata v1: "
_METADATA_SUFFIX = "."


CORE_UPSTREAM_IMAGES: frozenset[str] = frozenset(
    {
        "docker.getcollate.io/openmetadata/db@sha256:8a77669a2e64769dbb3ba4684fd527cc4a68e54879b199a6a8f1e74fa14da557",
        "docker.elastic.co/elasticsearch/elasticsearch@sha256:4f6bdcb742e892539c6ac49b0dd3e4e182e90218546e8c6a22db378c344acb60",
        "docker.getcollate.io/openmetadata/server@sha256:6c878281973d9e2c366e9da4f256a744acf67b1e53195fab67c3191e504e4169",
    }
)
UPSTREAM_IMAGES: frozenset[str] = CORE_UPSTREAM_IMAGES | {
    "docker.getcollate.io/openmetadata/ingestion@sha256:fe5effad9dbce98852b2f588905a4a8926c3c03de97fcceac8fe8e3ec927d717"
}


class OpenMetadataSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = Field(pattern=r"^https?://[^/]+$")
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)


@dataclass(frozen=True, slots=True)
class _OpenMetadataCredentials:
    username: str
    password: SecretStr
    runtime_password: SecretStr
    administrator_password: SecretStr

    def password_for_service_identity(self, identity: str) -> SecretStr:
        if identity == "runtime":
            return self.runtime_password
        if identity == "administrator":
            return self.administrator_password
        raise CatalogProviderError(
            "OpenMetadata service identity is unsupported", classification="invalid_request"
        )


@dataclass(frozen=True, slots=True)
class _RecordedProviderResource:
    resource_kind: Literal["namespace", "user", "object"]
    identifier: str
    collection: str


class _Transport(Protocol):
    def get(self, url: str, **kwargs: object) -> httpx.Response: ...

    def request(self, method: str, url: str, **kwargs: object) -> httpx.Response: ...


type EntityStatus = Literal[
    "Draft", "In Review", "Approved", "Archived", "Deprecated", "Rejected", "Unprocessed"
]
type ProviderKind = Literal["system", "user", "automation"]


def _tuple_from_json_array(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    return value


type _ResponseTuple[Item] = Annotated[tuple[Item, ...], BeforeValidator(_tuple_from_json_array)]


def _canonical_provider_id(value: object) -> object:
    if not isinstance(value, str):
        raise ValueError("provider ID must be a canonical UUID string")
    try:
        canonical_value = str(UUID(value))
    except ValueError as error:
        raise ValueError("provider ID must be a canonical UUID string") from error
    if canonical_value != value:
        raise ValueError("provider ID must be a canonical UUID string")
    return value


type _CanonicalProviderId = Annotated[str, BeforeValidator(_canonical_provider_id)]


class _EntityReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    id: _CanonicalProviderId
    type: str = Field(min_length=1)
    name: str | None = None
    fully_qualified_name: str | None = Field(default=None, alias="fullyQualifiedName")
    description: str | None = None
    display_name: str | None = Field(default=None, alias="displayName")
    deleted: bool | None = None
    inherited: bool | None = None
    href: str | None = None


class _SearchDocumentResponse(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, strict=True)

    provider_id: _CanonicalProviderId = Field(alias="id")


class _TagReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    tag_fqn: str = Field(alias="tagFQN", min_length=1)
    name: str | None = None
    display_name: str | None = Field(default=None, alias="displayName")
    description: str | None = None
    source: Literal["Classification", "Glossary"] | None = None
    label_type: Literal["Manual", "Propagated", "Automated", "Derived", "Generated"] | None = Field(
        default=None, alias="labelType"
    )
    state: Literal["Suggested", "Confirmed"] | None = None
    href: str | None = None
    applied_at: int | None = Field(default=None, alias="appliedAt")


class _BasicAuthenticationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    password: SecretStr


class _BasicAuthenticationMechanism(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    auth_type: Literal["BASIC"] = Field(alias="authType")
    config: _BasicAuthenticationConfig


class _PersonaLandingPageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    header_color: str | None = Field(default=None, alias="headerColor")
    header_image: str | None = Field(default=None, alias="headerImage")


class _PersonaPreference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    persona_id: _CanonicalProviderId = Field(alias="personaId")
    persona_name: str = Field(alias="personaName", min_length=1)
    landing_page_settings: _PersonaLandingPageSettings | None = Field(
        default=None, alias="landingPageSettings"
    )


class _FieldChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    name: str | None = None
    old_value: JsonValue | None = Field(default=None, alias="oldValue")
    new_value: JsonValue | None = Field(default=None, alias="newValue")


class _ChangeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    change_source: (
        Literal["Manual", "Propagated", "Automated", "Derived", "Ingested", "Suggested"] | None
    ) = Field(default=None, alias="changeSource")
    changed_by: str | None = Field(default=None, alias="changedBy")
    changed_at: int | None = Field(default=None, alias="changedAt")


class _ChangeDescription(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    fields_added: _ResponseTuple[_FieldChange] | None = Field(default=None, alias="fieldsAdded")
    fields_updated: _ResponseTuple[_FieldChange] | None = Field(default=None, alias="fieldsUpdated")
    fields_deleted: _ResponseTuple[_FieldChange] | None = Field(default=None, alias="fieldsDeleted")
    previous_version: float | None = Field(default=None, alias="previousVersion")
    change_summary: Mapping[str, _ChangeSummary] | None = Field(default=None, alias="changeSummary")


class _ConceptMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    concept_iri: AnyUrl = Field(alias="conceptIri")
    mapping_type: Literal[
        "EXACT_MATCH",
        "CLOSE_MATCH",
        "BROAD_MATCH",
        "NARROW_MATCH",
        "RELATED_MATCH",
        "SAME_AS",
    ] = Field(alias="mappingType")
    scheme_iri: AnyUrl | None = Field(default=None, alias="schemeIri")
    source: str | None = None


class _TermReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str | None = None
    endpoint: AnyUrl | None = None


class _TermRelation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    relation_type: str = Field(
        default="relatedTo", alias="relationType", pattern=r"^[a-zA-Z][a-zA-Z0-9]*$"
    )
    term: _EntityReference


class _Votes(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    up_votes: int = Field(default=0, alias="upVotes")
    down_votes: int = Field(default=0, alias="downVotes")
    up_voters: _ResponseTuple[_EntityReference] | None = Field(default=None, alias="upVoters")
    down_voters: _ResponseTuple[_EntityReference] | None = Field(default=None, alias="downVoters")


type _ClassificationLanguage = Literal[
    "any",
    "af",
    "sq",
    "am",
    "ar",
    "hy",
    "az",
    "eu",
    "be",
    "bn",
    "bs",
    "bg",
    "ca",
    "zh",
    "hr",
    "cs",
    "da",
    "nl",
    "en",
    "et",
    "fi",
    "fr",
    "gl",
    "ka",
    "de",
    "el",
    "gu",
    "ht",
    "he",
    "hi",
    "hu",
    "is",
    "id",
    "ga",
    "it",
    "ja",
    "kn",
    "kk",
    "km",
    "ko",
    "ku",
    "ky",
    "lo",
    "lv",
    "lt",
    "mk",
    "ms",
    "ml",
    "mt",
    "mi",
    "mr",
    "mn",
    "my",
    "ne",
    "no",
    "ps",
    "fa",
    "pl",
    "pt",
    "pa",
    "ro",
    "ru",
    "sr",
    "si",
    "sk",
    "sl",
    "so",
    "es",
    "sw",
    "sv",
    "tl",
    "ta",
    "te",
    "th",
    "tr",
    "uk",
    "ur",
    "uz",
    "vi",
    "cy",
    "yi",
    "zu",
]
type _PiiEntity = Literal[
    "CREDIT_CARD",
    "CRYPTO",
    "DATE_TIME",
    "EMAIL_ADDRESS",
    "IBAN_CODE",
    "IP_ADDRESS",
    "NRP",
    "LOCATION",
    "PERSON",
    "PHONE_NUMBER",
    "MEDICAL_LICENSE",
    "URL",
    "US_BANK_NUMBER",
    "US_DRIVER_LICENSE",
    "US_ITIN",
    "US_PASSPORT",
    "US_SSN",
    "UK_NHS",
    "ES_NIF",
    "ES_NIE",
    "IT_FISCAL_CODE",
    "IT_DRIVER_LICENSE",
    "IT_VAT_CODE",
    "IT_PASSPORT",
    "IT_IDENTITY_CARD",
    "PL_PESEL",
    "SG_NRIC_FIN",
    "SG_UEN",
    "AU_ABN",
    "AU_ACN",
    "AU_TFN",
    "AU_MEDICARE",
    "IN_PAN",
    "IN_AADHAAR",
    "IN_VEHICLE_REGISTRATION",
    "IN_VOTER",
    "IN_PASSPORT",
    "FI_PERSONAL_IDENTITY_CODE",
]
type _PredefinedRecognizerName = Literal[
    "AbaRoutingRecognizer",
    "CreditCardRecognizer",
    "CryptoRecognizer",
    "DateRecognizer",
    "EmailRecognizer",
    "IbanRecognizer",
    "IpRecognizer",
    "NhsRecognizer",
    "MedicalLicenseRecognizer",
    "PhoneRecognizer",
    "SgFinRecognizer",
    "UrlRecognizer",
    "UsBankRecognizer",
    "UsItinRecognizer",
    "UsLicenseRecognizer",
    "UsPassportRecognizer",
    "UsSsnRecognizer",
    "EsNifRecognizer",
    "SpacyRecognizer",
    "StanzaRecognizer",
    "AuAbnRecognizer",
    "AuAcnRecognizer",
    "AuTfnRecognizer",
    "AuMedicareRecognizer",
    "TransformersRecognizer",
    "ItDriverLicenseRecognizer",
    "ItFiscalCodeRecognizer",
    "ItVatCodeRecognizer",
    "ItIdentityCardRecognizer",
    "ItPassportRecognizer",
    "InPanRecognizer",
    "GLiNERRecognizer",
    "PlPeselRecognizer",
    "AzureAILanguageRecognizer",
    "InAadhaarRecognizer",
    "InVehicleRegistrationRecognizer",
    "SgUenRecognizer",
    "InVoterRecognizer",
    "InPassportRecognizer",
    "FiPersonalIdentityCodeRecognizer",
    "EsNieRecognizer",
    "UkNinoRecognizer",
]


class _RegexFlags(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    dot_all: bool | None = Field(default=None, alias="dotAll")
    multiline: bool | None = None
    ignore_case: bool | None = Field(default=None, alias="ignoreCase")


class _RecognizerPattern(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    regex: str
    score: float | None = Field(default=None, ge=0, le=1)


class _PatternRecognizerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    type: Literal["pattern"]
    patterns: _ResponseTuple[_RecognizerPattern]
    regex_flags: _RegexFlags = Field(alias="regexFlags")
    context: _ResponseTuple[str] = ()
    supported_language: _ClassificationLanguage = Field(alias="supportedLanguage")


class _ExactTermsRecognizerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    type: Literal["exact_terms"]
    exact_terms: _ResponseTuple[str] = Field(min_length=1, alias="exactTerms")
    supported_language: _ClassificationLanguage = Field(alias="supportedLanguage")
    regex_flags: _RegexFlags = Field(alias="regexFlags")


class _ContextRecognizerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    type: Literal["context"]
    context_words: _ResponseTuple[str] = Field(min_length=1, alias="contextWords")
    supported_language: _ClassificationLanguage = Field(alias="supportedLanguage")
    min_score: float | None = Field(default=None, alias="minScore", ge=0, le=1)
    max_score: float | None = Field(default=None, alias="maxScore", ge=0, le=1)
    increase_factor_by_char_length: float | None = Field(
        default=None, alias="increaseFactorByCharLength"
    )


class _CustomRecognizerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    type: Literal["custom"]
    validator_function: str = Field(alias="validatorFunction")
    config: Mapping[str, JsonValue] | None = None
    supported_language: _ClassificationLanguage = Field(alias="supportedLanguage")


class _PredefinedRecognizerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    type: Literal["predefined"]
    name: _PredefinedRecognizerName
    supported_language: _ClassificationLanguage = Field(default="en", alias="supportedLanguage")
    context: _ResponseTuple[str] = ()
    supported_entities: _ResponseTuple[_PiiEntity] | None = Field(
        default=None, alias="supportedEntities"
    )


type _RecognizerConfig = Annotated[
    _PatternRecognizerConfig
    | _ExactTermsRecognizerConfig
    | _ContextRecognizerConfig
    | _CustomRecognizerConfig
    | _PredefinedRecognizerConfig,
    Field(discriminator="type"),
]


class _RecognizerException(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    entity_link: str = Field(alias="entityLink", min_length=1)
    reason: str | None = None
    added_by: _EntityReference | None = Field(default=None, alias="addedBy")
    added_at: int | None = Field(default=None, alias="addedAt")
    feedback_id: _CanonicalProviderId | None = Field(default=None, alias="feedbackId")


class _Recognizer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    name: str = Field(min_length=1)
    recognizer_config: _RecognizerConfig = Field(alias="recognizerConfig")
    id: _CanonicalProviderId | None = None
    display_name: str | None = Field(default=None, alias="displayName")
    description: str | None = None
    enabled: bool | None = None
    is_system_default: bool | None = Field(default=None, alias="isSystemDefault")
    confidence_threshold: float | None = Field(
        default=None, alias="confidenceThreshold", ge=0, le=1
    )
    exception_list: _ResponseTuple[_RecognizerException] = Field(default=(), alias="exceptionList")
    version: float | None = None
    updated_at: int | None = Field(default=None, alias="updatedAt")
    updated_by: str | None = Field(default=None, alias="updatedBy")
    target: Literal["content", "column_name"] | None = None


class _EntityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    id: _CanonicalProviderId
    name: str = Field(min_length=1)
    description: str | None = None
    fully_qualified_name: str | None = Field(default=None, alias="fullyQualifiedName")
    display_name: str | None = Field(default=None, alias="displayName")
    version: float | None = None
    updated_at: int | None = Field(default=None, alias="updatedAt")
    updated_by: str | None = Field(default=None, alias="updatedBy")
    href: str | None = None
    deleted: bool | None = None
    extension: Mapping[str, str] | None = None


class _ProviderManagedEntityResponse(_EntityResponse):
    provider: ProviderKind | None = None
    disabled: bool | None = None


class _GovernedEntityResponse(_ProviderManagedEntityResponse):
    entity_status: EntityStatus | None = Field(default=None, alias="entityStatus")
    owners: _ResponseTuple[_EntityReference] | None = None
    reviewers: _ResponseTuple[_EntityReference] | None = None


class _TaggedEntityResponse(_GovernedEntityResponse):
    tags: _ResponseTuple[_TagReference] = ()

    @property
    def tag_fqns(self) -> tuple[str, ...]:
        return tuple(tag.tag_fqn for tag in self.tags)


class _GlossaryResponse(_TaggedEntityResponse):
    usage_count: int | None = Field(default=None, alias="usageCount")
    term_count: int | None = Field(default=None, alias="termCount")
    mutually_exclusive: bool | None = Field(default=None, alias="mutuallyExclusive")
    domains: _ResponseTuple[_EntityReference] | None = None
    data_products: _ResponseTuple[_EntityReference] | None = Field(
        default=None, alias="dataProducts"
    )
    votes: _Votes | None = None
    change_description: _ChangeDescription | None = Field(default=None, alias="changeDescription")
    incremental_change_description: _ChangeDescription | None = Field(
        default=None, alias="incrementalChangeDescription"
    )


class _GlossaryTermResponse(_TaggedEntityResponse):
    glossary: _EntityReference | None = None
    parent: _EntityReference | None = None
    children: _ResponseTuple[_EntityReference] | None = None
    synonyms: _ResponseTuple[str] | None = None
    references: _ResponseTuple[_TermReference] | None = None
    concept_mappings: _ResponseTuple[_ConceptMapping] = Field(default=(), alias="conceptMappings")
    related_terms: _ResponseTuple[_TermRelation] = Field(default=(), alias="relatedTerms")
    usage_count: int | None = Field(default=None, alias="usageCount")
    mutually_exclusive: bool | None = Field(default=None, alias="mutuallyExclusive")
    domains: _ResponseTuple[_EntityReference] | None = None
    data_products: _ResponseTuple[_EntityReference] | None = Field(
        default=None, alias="dataProducts"
    )
    votes: _Votes | None = None
    change_description: _ChangeDescription | None = Field(default=None, alias="changeDescription")
    incremental_change_description: _ChangeDescription | None = Field(
        default=None, alias="incrementalChangeDescription"
    )


class _DomainResponse(_TaggedEntityResponse):
    domain_type: Literal["Source-aligned", "Consumer-aligned", "Aggregate"] = Field(
        alias="domainType"
    )
    parent: _EntityReference | None = None
    children: _ResponseTuple[_EntityReference] | None = None
    children_count: int | None = Field(default=None, alias="childrenCount")
    experts: _ResponseTuple[_EntityReference] | None = None
    assets: _ResponseTuple[_EntityReference] | None = None
    followers: _ResponseTuple[_EntityReference] | None = None
    votes: _Votes | None = None
    certification: JsonValue | None = None
    change_description: _ChangeDescription | None = Field(default=None, alias="changeDescription")
    incremental_change_description: _ChangeDescription | None = Field(
        default=None, alias="incrementalChangeDescription"
    )


class _DataProductResponse(_TaggedEntityResponse):
    domains: _ResponseTuple[_EntityReference] = Field(min_length=1)
    experts: _ResponseTuple[_EntityReference] | None = None
    assets: _ResponseTuple[_EntityReference] | None = None
    lifecycle_stage: (
        Literal[
            "IDEATION", "DESIGN", "DEVELOPMENT", "TESTING", "PRODUCTION", "DEPRECATED", "RETIRED"
        ]
        | None
    ) = Field(default=None, alias="lifecycleStage")
    data_product_type: (
        Literal[
            "RAW_DATA",
            "DERIVED_DATA",
            "DATASET",
            "REPORTS",
            "ANALYTIC_VIEW",
            "VISUALISATION_3D",
            "ALGORITHM",
            "DECISION_SUPPORT",
            "AUTOMATED_DECISION_MAKING",
            "DATA_ENHANCED_PRODUCT",
            "DATA_DRIVEN_SERVICE",
            "DATA_ENABLED_PERFORMANCE",
            "BI_DIRECTIONAL",
        ]
        | None
    ) = Field(default=None, alias="dataProductType")
    visibility: Literal["PRIVATE", "INVITATION", "ORGANISATION", "DATASPACE", "PUBLIC"] | None = (
        None
    )
    portfolio_priority: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"] | None = Field(
        default=None, alias="portfolioPriority"
    )
    sla: JsonValue | None = None
    consumes_from: _ResponseTuple[_EntityReference] | None = Field(
        default=None, alias="consumesFrom"
    )
    provides_to: _ResponseTuple[_EntityReference] | None = Field(default=None, alias="providesTo")
    followers: _ResponseTuple[_EntityReference] | None = None
    votes: _Votes | None = None
    certification: JsonValue | None = None
    change_description: _ChangeDescription | None = Field(default=None, alias="changeDescription")
    incremental_change_description: _ChangeDescription | None = Field(
        default=None, alias="incrementalChangeDescription"
    )


class _DatabaseServiceResponse(_TaggedEntityResponse):
    service_type: Literal["Postgres", "Clickhouse"] = Field(alias="serviceType")
    connection: JsonValue | None = None
    pipelines: _ResponseTuple[_EntityReference] | None = None
    domains: _ResponseTuple[_EntityReference] | None = None
    data_products: _ResponseTuple[_EntityReference] | None = Field(
        default=None, alias="dataProducts"
    )
    test_connection_result: JsonValue | None = Field(default=None, alias="testConnectionResult")
    ingestion_runner: _EntityReference | None = Field(default=None, alias="ingestionRunner")
    followers: _ResponseTuple[_EntityReference] | None = None
    impersonated_by: _EntityReference | None = Field(default=None, alias="impersonatedBy")
    data_contract: JsonValue | None = Field(default=None, alias="dataContract")
    change_description: _ChangeDescription | None = Field(default=None, alias="changeDescription")
    incremental_change_description: _ChangeDescription | None = Field(
        default=None, alias="incrementalChangeDescription"
    )


class _DatabaseResponse(_TaggedEntityResponse):
    service: _EntityReference
    service_type: Literal["Postgres", "Clickhouse"] | None = Field(
        default=None, alias="serviceType"
    )
    database_schemas: _ResponseTuple[_EntityReference] | None = Field(
        default=None, alias="databaseSchemas"
    )
    default: bool | None = None
    retention_period: str | None = Field(default=None, alias="retentionPeriod")
    source_url: str | None = Field(default=None, alias="sourceUrl")
    domains: _ResponseTuple[_EntityReference] | None = None
    data_products: _ResponseTuple[_EntityReference] | None = Field(
        default=None, alias="dataProducts"
    )
    life_cycle: JsonValue | None = Field(default=None, alias="lifeCycle")
    source_hash: str | None = Field(default=None, alias="sourceHash")
    location: _EntityReference | None = None
    followers: _ResponseTuple[_EntityReference] | None = None
    impersonated_by: _EntityReference | None = Field(default=None, alias="impersonatedBy")
    database_profiler_config: JsonValue | None = Field(default=None, alias="databaseProfilerConfig")
    data_contract: JsonValue | None = Field(default=None, alias="dataContract")
    usage_summary: JsonValue | None = Field(default=None, alias="usageSummary")
    votes: _Votes | None = None
    certification: JsonValue | None = None
    change_description: _ChangeDescription | None = Field(default=None, alias="changeDescription")
    incremental_change_description: _ChangeDescription | None = Field(
        default=None, alias="incrementalChangeDescription"
    )


class _DatabaseSchemaResponse(_TaggedEntityResponse):
    database: _EntityReference
    service: _EntityReference | None = None
    service_type: Literal["Postgres", "Clickhouse"] | None = Field(
        default=None, alias="serviceType"
    )
    tables: _ResponseTuple[_EntityReference] | None = None
    retention_period: str | None = Field(default=None, alias="retentionPeriod")
    source_url: str | None = Field(default=None, alias="sourceUrl")
    domains: _ResponseTuple[_EntityReference] | None = None
    data_products: _ResponseTuple[_EntityReference] | None = Field(
        default=None, alias="dataProducts"
    )
    life_cycle: JsonValue | None = Field(default=None, alias="lifeCycle")
    source_hash: str | None = Field(default=None, alias="sourceHash")
    followers: _ResponseTuple[_EntityReference] | None = None
    impersonated_by: _EntityReference | None = Field(default=None, alias="impersonatedBy")
    database_schema_profiler_config: JsonValue | None = Field(
        default=None, alias="databaseSchemaProfilerConfig"
    )
    data_contract: JsonValue | None = Field(default=None, alias="dataContract")
    usage_summary: JsonValue | None = Field(default=None, alias="usageSummary")
    votes: _Votes | None = None
    certification: JsonValue | None = None
    change_description: _ChangeDescription | None = Field(default=None, alias="changeDescription")
    incremental_change_description: _ChangeDescription | None = Field(
        default=None, alias="incrementalChangeDescription"
    )


class _TableColumnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    name: str = Field(min_length=1)
    display_name: str | None = Field(default=None, alias="displayName")
    data_type: str = Field(alias="dataType", min_length=1)
    array_data_type: str | None = Field(default=None, alias="arrayDataType")
    data_length: int | None = Field(default=None, alias="dataLength")
    precision: int | None = None
    scale: int | None = None
    data_type_display: str | None = Field(default=None, alias="dataTypeDisplay")
    description: str | None = None
    fully_qualified_name: str | None = Field(default=None, alias="fullyQualifiedName")
    tags: _ResponseTuple[_TagReference] = ()
    constraint: Literal["NULL", "NOT_NULL", "UNIQUE", "PRIMARY_KEY"] | None = None
    ordinal_position: int | None = Field(default=None, alias="ordinalPosition")
    json_schema: str | None = Field(default=None, alias="jsonSchema")
    children: _ResponseTuple[_TableColumnResponse] | None = None
    profile: JsonValue | None = None
    custom_metrics: _ResponseTuple[JsonValue] | None = Field(default=None, alias="customMetrics")
    extension: Mapping[str, JsonValue] | None = None


class _TableResponse(_TaggedEntityResponse):
    table_type: str | None = Field(default=None, alias="tableType")
    columns: _ResponseTuple[_TableColumnResponse]
    database_schema: _EntityReference = Field(alias="databaseSchema")
    database: _EntityReference | None = None
    service: _EntityReference | None = None
    service_type: Literal["Postgres", "Clickhouse"] | None = Field(
        default=None, alias="serviceType"
    )
    owners: _ResponseTuple[_EntityReference] | None = None
    domains: _ResponseTuple[_EntityReference] | None = None
    data_products: _ResponseTuple[_EntityReference] | None = Field(
        default=None, alias="dataProducts"
    )
    table_constraints: _ResponseTuple[JsonValue] | None = Field(
        default=None, alias="tableConstraints"
    )
    table_partition: JsonValue | None = Field(default=None, alias="tablePartition")
    table_profiler_config: JsonValue | None = Field(default=None, alias="tableProfilerConfig")
    location_path: str | None = Field(default=None, alias="locationPath")
    schema_definition: str | None = Field(default=None, alias="schemaDefinition")
    retention_period: str | None = Field(default=None, alias="retentionPeriod")
    source_url: str | None = Field(default=None, alias="sourceUrl")
    file_format: str | None = Field(default=None, alias="fileFormat")
    life_cycle: JsonValue | None = Field(default=None, alias="lifeCycle")
    source_hash: str | None = Field(default=None, alias="sourceHash")
    data_model: JsonValue | None = Field(default=None, alias="dataModel")
    compression_codec: str | None = Field(default=None, alias="compressionCodec")
    compression_enabled: bool | None = Field(default=None, alias="compressionEnabled")
    compression_strategy: str | None = Field(default=None, alias="compressionStrategy")
    custom_metrics: _ResponseTuple[JsonValue] | None = Field(default=None, alias="customMetrics")
    data_contract: JsonValue | None = Field(default=None, alias="dataContract")
    followers: _ResponseTuple[_EntityReference] | None = None
    impersonated_by: _EntityReference | None = Field(default=None, alias="impersonatedBy")
    joins: JsonValue | None = None
    location: _EntityReference | None = None
    pipeline_observability: JsonValue | None = Field(default=None, alias="pipelineObservability")
    processed_lineage: bool | None = Field(default=None, alias="processedLineage")
    profile: JsonValue | None = None
    queries: _ResponseTuple[JsonValue] | None = None
    sample_data: JsonValue | None = Field(default=None, alias="sampleData")
    test_suite: _EntityReference | None = Field(default=None, alias="testSuite")
    usage_summary: JsonValue | None = Field(default=None, alias="usageSummary")
    votes: _Votes | None = None
    certification: JsonValue | None = None
    change_description: _ChangeDescription | None = Field(default=None, alias="changeDescription")
    incremental_change_description: _ChangeDescription | None = Field(
        default=None, alias="incrementalChangeDescription"
    )


class _AutoClassificationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    enabled: bool
    conflict_resolution: Literal["highest_confidence", "highest_priority", "most_specific"] = Field(
        alias="conflictResolution"
    )
    minimum_confidence: float = Field(alias="minimumConfidence", ge=0, le=1)
    require_explicit_match: bool = Field(alias="requireExplicitMatch")


class _ClassificationResponse(_GovernedEntityResponse):
    usage_count: int | None = Field(default=None, alias="usageCount")
    term_count: int | None = Field(default=None, alias="termCount")
    mutually_exclusive: bool | None = Field(default=None, alias="mutuallyExclusive")
    domains: _ResponseTuple[_EntityReference] | None = None
    auto_classification_config: _AutoClassificationConfig | None = Field(
        default=None, alias="autoClassificationConfig"
    )


class _TagResponse(_GovernedEntityResponse):
    classification: _EntityReference | None = None
    parent: _EntityReference | None = None
    children: _ResponseTuple[_EntityReference] | None = None
    usage_count: int | None = Field(default=None, alias="usageCount")
    deprecated: bool | None = None
    mutually_exclusive: bool | None = Field(default=None, alias="mutuallyExclusive")
    domains: _ResponseTuple[_EntityReference] | None = None
    data_products: _ResponseTuple[_EntityReference] | None = Field(
        default=None, alias="dataProducts"
    )
    recognizers: _ResponseTuple[_Recognizer] | None = None
    auto_classification_enabled: bool | None = Field(
        default=None, alias="autoClassificationEnabled"
    )
    auto_classification_priority: int | None = Field(
        default=None, alias="autoClassificationPriority", ge=0, le=100
    )


class _UserResponse(_EntityResponse):
    email: str | None = None
    is_bot: bool | None = Field(default=None, alias="isBot")
    is_admin: bool | None = Field(default=None, alias="isAdmin")
    allow_impersonation: bool | None = Field(default=None, alias="allowImpersonation")
    authentication_mechanism: _BasicAuthenticationMechanism | None = Field(
        default=None, alias="authenticationMechanism"
    )
    teams: _ResponseTuple[_EntityReference] | None = None
    roles: _ResponseTuple[_EntityReference] | None = None
    inherited_roles: _ResponseTuple[_EntityReference] | None = Field(
        default=None, alias="inheritedRoles"
    )
    inherited_personas: _ResponseTuple[_EntityReference] | None = Field(
        default=None, alias="inheritedPersonas"
    )
    personas: _ResponseTuple[_EntityReference] | None = None
    domains: _ResponseTuple[_EntityReference] | None = None
    persona_preferences: _ResponseTuple[_PersonaPreference] = Field(
        default=(), alias="personaPreferences"
    )
    change_description: _ChangeDescription | None = Field(default=None, alias="changeDescription")
    incremental_change_description: _ChangeDescription | None = Field(
        default=None, alias="incrementalChangeDescription"
    )


class _PolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    name: str = Field(min_length=1)
    fully_qualified_name: str | None = Field(default=None, alias="fullyQualifiedName")
    description: str | None = None
    effect: Literal["allow", "deny"]
    operations: _ResponseTuple[str]
    resources: _ResponseTuple[str]
    condition: str | None = None


class _PolicyResponse(_ProviderManagedEntityResponse):
    enabled: bool | None = None
    rules: _ResponseTuple[_PolicyRule] | None = None
    allow_delete: bool | None = Field(default=None, alias="allowDelete")
    allow_edit: bool | None = Field(default=None, alias="allowEdit")
    owners: _ResponseTuple[_EntityReference] | None = None
    roles: _ResponseTuple[_EntityReference] | None = None
    teams: _ResponseTuple[_EntityReference] | None = None
    domains: _ResponseTuple[_EntityReference] | None = None


class _RoleResponse(_ProviderManagedEntityResponse):
    allow_delete: bool | None = Field(default=None, alias="allowDelete")
    allow_edit: bool | None = Field(default=None, alias="allowEdit")
    policies: _ResponseTuple[_EntityReference] | None = None
    users: _ResponseTuple[_EntityReference] | None = None
    teams: _ResponseTuple[_EntityReference] | None = None
    domains: _ResponseTuple[_EntityReference] | None = None


class _LineageEndpointIdentifier(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: UUID
    type: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9]*$")


class _LineageResourceIdentifier(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    from_entity: _LineageEndpointIdentifier = Field(alias="fromEntity")
    to_entity: _LineageEndpointIdentifier = Field(alias="toEntity")
    description: str = Field(min_length=1)


class _LineageStableReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    identity: str = Field(min_length=1)
    from_ref: str = Field(min_length=1)
    to_ref: str = Field(min_length=1)


class _ColumnLineageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    from_columns: _ResponseTuple[str] | None = Field(default=None, alias="fromColumns")
    to_column: str | None = Field(default=None, alias="toColumn")
    function: str | None = None


class _TemporaryLineageTableResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    from_entity: str = Field(alias="fromEntity", min_length=1)
    to_entity: str = Field(alias="toEntity", min_length=1)


class _LineageDetailsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    sql_query: str | None = Field(default=None, alias="sqlQuery")
    columns_lineage: _ResponseTuple[_ColumnLineageResponse] | None = Field(
        default=None, alias="columnsLineage"
    )
    pipeline: _EntityReference | None = None
    description: str | None = None
    source: (
        Literal[
            "Manual",
            "ViewLineage",
            "QueryLineage",
            "PipelineLineage",
            "DashboardLineage",
            "DbtLineage",
            "SparkLineage",
            "OpenLineage",
            "ExternalTableLineage",
            "CrossDatabaseLineage",
            "ChildAssets",
        ]
        | None
    ) = None
    created_at: int | None = Field(default=None, alias="createdAt")
    created_by: str | None = Field(default=None, alias="createdBy")
    updated_at: int | None = Field(default=None, alias="updatedAt")
    updated_by: str | None = Field(default=None, alias="updatedBy")
    asset_edges: int | None = Field(default=None, alias="assetEdges")
    temporary_lineage_tables: _ResponseTuple[_TemporaryLineageTableResponse] | None = Field(
        default=None, alias="tempLineageTables"
    )


class _LineageEdgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    edge: _LineageDetailsResponse


class _ObservedLineageEdgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    from_entity: _CanonicalProviderId = Field(alias="fromEntity")
    to_entity: _CanonicalProviderId = Field(alias="toEntity")
    description: str | None = None
    lineage_details: _LineageDetailsResponse | None = Field(default=None, alias="lineageDetails")

    @property
    def observed_description(self) -> str | None:
        if self.description is not None:
            return self.description
        if self.lineage_details is None:
            return None
        return self.lineage_details.description


class _EntityLineageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    entity: _EntityReference
    nodes: _ResponseTuple[_EntityReference] | None = None
    upstream_edges: _ResponseTuple[_ObservedLineageEdgeResponse] | None = Field(
        default=None, alias="upstreamEdges"
    )
    downstream_edges: _ResponseTuple[_ObservedLineageEdgeResponse] | None = Field(
        default=None, alias="downstreamEdges"
    )


class _HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "healthy"]


class _VersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["1.13.3"]
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    timestamp: int = Field(gt=0)


_ENTITY_RESPONSE_MODELS: Mapping[str, type[_EntityResponse]] = {
    "classifications": _ClassificationResponse,
    "databaseSchemas": _DatabaseSchemaResponse,
    "databases": _DatabaseResponse,
    "databaseServices": _DatabaseServiceResponse,
    "dataProducts": _DataProductResponse,
    "domains": _DomainResponse,
    "glossaries": _GlossaryResponse,
    "glossaryTerms": _GlossaryTermResponse,
    "policies": _PolicyResponse,
    "roles": _RoleResponse,
    "tags": _TagResponse,
    "tables": _TableResponse,
    "users": _UserResponse,
}


class _ObjectNotFoundError(CatalogProviderError):
    pass


class _LoginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    access_token: str = Field(alias="accessToken", min_length=1)
    refresh_token: str | None = Field(default=None, alias="refreshToken")
    token_type: str | None = Field(default=None, alias="tokenType")
    expiry_duration: int | None = Field(default=None, alias="expiryDuration")


class OpenMetadataClient:
    def __init__(
        self,
        *,
        settings: OpenMetadataSettings,
        credentials: _OpenMetadataCredentials | None = None,
        transport: _Transport | None = None,
    ) -> None:
        self._settings = settings
        self._credentials = credentials
        self._transport = transport or httpx.Client(timeout=settings.timeout_seconds)
        self._remote_identifiers: dict[tuple[str, str], tuple[str, str]] = {}
        self._recorded_provider_resources: dict[tuple[str, str], _RecordedProviderResource] = {}
        self._bearer_token: str | None = None

    def health(self) -> ProviderHealth:
        response = self._get("/api/v1/system/health")
        if response.text.strip() == "OK":
            return ProviderHealth()
        response_is_valid = True
        try:
            _HealthResponse.model_validate(self._payload(response))
        except ValidationError:
            response_is_valid = False
        if not response_is_valid:
            raise CatalogProviderError(
                "OpenMetadata health response failed provider validation",
                classification="permanent",
            )
        return ProviderHealth()

    def build_identity(self) -> ProviderBuildIdentity:
        try:
            observed = _VersionResponse.model_validate(
                self._payload(self._get("/api/v1/system/version"))
            )
        except ValidationError:
            raise CatalogProviderError(
                "OpenMetadata version response failed provider validation",
                classification="permanent",
            ) from None
        return ProviderBuildIdentity(
            provider_version=observed.version,
            revision=observed.revision,
            build_timestamp=observed.timestamp,
        )

    def assert_object_searchable(self, *, tenant_key: str, identity: str) -> None:
        expected_identifier = self._resolve_glossary_term_identifier(
            tenant_key=tenant_key,
            reference=_stable_identity("glossary_term", tenant_key, identity),
        )[0]
        response: httpx.Response | None = None
        with suppress(_ObjectNotFoundError):
            response = self._get(
                "/api/v1/search/get/glossary_term_search_index/doc/" + expected_identifier
            )
        search_document: _SearchDocumentResponse | None = None
        if response is not None:
            with suppress(ValidationError):
                search_document = _SearchDocumentResponse.model_validate(self._payload(response))
        if search_document is None or search_document.provider_id != expected_identifier:
            raise CatalogProviderError(
                "OpenMetadata restored metadata failed search verification",
                classification="permanent",
            )

    def assert_object_absent(self, *, tenant_key: str, identity: str) -> None:
        try:
            self.get_object(tenant_key=tenant_key, identity=identity)
        except CatalogProviderError as error:
            if (
                error.classification == "permanent"
                and str(error) == "OpenMetadata object was not found"
            ):
                return
            raise
        raise CatalogProviderError(
            "OpenMetadata unexpected object was present", classification="permanent"
        )

    def rotate_admin_password(self, new_password: SecretStr) -> None:
        credentials = self._credentials
        if credentials is None:
            raise CatalogProviderError(
                "OpenMetadata private credentials are unavailable", classification="authentication"
            )
        response = self._request(
            "PUT",
            "/api/v1/users/changePassword",
            json_payload={
                "oldPassword": credentials.password.get_secret_value(),
                "newPassword": new_password.get_secret_value(),
                "confirmPassword": new_password.get_secret_value(),
                "requestType": "SELF",
            },
        )
        self._password_change_acknowledgement(response)
        self._credentials = replace(credentials, password=new_password)
        self._bearer_token = None

    def rotate_service_identity_password(
        self, *, tenant_key: str, identity: str, new_password: SecretStr
    ) -> None:
        if identity not in {"runtime", "administrator"}:
            raise CatalogProviderError(
                "OpenMetadata service identity is unsupported",
                classification="invalid_request",
            )
        password = new_password.get_secret_value()
        response = self._request(
            "PUT",
            "/api/v1/users/changePassword",
            json_payload={
                "username": _object_name(tenant_key, identity),
                "newPassword": password,
                "confirmPassword": password,
                "requestType": "USER",
            },
        )
        self._password_change_acknowledgement(response)

    def ensure_tenant_namespace(self, *, tenant_key: str, idempotency_key: str) -> CatalogObjectRef:
        name = _object_name(tenant_key, "namespace")
        entity = self._ensure_entity(
            collection="glossaries",
            name=name,
            payload={
                "name": name,
                "displayName": f"PillarMesh {tenant_key}",
                "description": "PillarMesh managed tenant catalog namespace.",
            },
        )
        reference = _object_ref("namespace", tenant_key, "namespace", entity)
        self._remember(reference, entity, entity_type="glossary")
        return reference

    def ensure_service_identity(
        self, *, tenant_key: str, identity: str, idempotency_key: str
    ) -> CatalogObjectRef:
        name = _object_name(tenant_key, identity)
        password = self._service_identity_password(identity)
        role_ids: list[str] = []
        if identity == "runtime":
            role_ids.append(self._ensure_runtime_role(tenant_key).id)
        entity = self._ensure_entity(
            collection="users",
            name=name,
            payload={
                "name": name,
                "email": _service_identity_username(tenant_key, identity),
                "password": password,
                "confirmPassword": password,
                "createPasswordType": "ADMIN_CREATE",
                "isAdmin": identity == "administrator",
                "roles": role_ids,
            },
        )
        reference = _object_ref("service_identity", tenant_key, identity, entity)
        self._remember(reference, entity, entity_type="user")
        return reference

    def ensure_glossary_term(
        self,
        *,
        tenant_key: str,
        identity: str,
        payload: GlossaryTermPayload,
        idempotency_key: str,
    ) -> CatalogObjectRef:
        namespace_name = _object_name(tenant_key, "namespace")
        namespace = self._get_entity(
            "glossaries",
            namespace_name,
            expected_name=namespace_name,
            expected_fqn=namespace_name,
        )
        owner_name = _object_name(tenant_key, payload.owner_ref)
        owner = self._get_entity("users", owner_name, expected_name=owner_name)
        name = _object_name(tenant_key, identity)
        entity_payload: dict[str, object] = {
            "name": name,
            "displayName": payload.name,
            "description": _description_with_metadata(
                payload.definition,
                {
                    "owner_ref": payload.owner_ref,
                    "provenance_ref": payload.provenance_ref,
                },
            ),
            "glossary": namespace.name,
            "owners": [{"id": owner.id, "type": "user"}],
        }
        entity = self._ensure_governed_glossary_term(
            name=name,
            lookup_name=f"{namespace.name}.{name}",
            payload=entity_payload,
        )
        reference = _object_ref("glossary_term", tenant_key, identity, entity)
        self._remember(reference, entity, entity_type="glossaryTerm")
        return reference

    def ensure_product_catalog_snapshot(
        self, definition: CatalogProductDefinition
    ) -> CatalogProductObservation:
        definition = CatalogProductDefinition.model_validate(
            definition.model_dump(mode="python"), strict=True
        )
        namespace_name = _object_name(definition.tenant_id, "namespace")
        self._get_entity(
            "glossaries",
            namespace_name,
            expected_name=namespace_name,
            expected_fqn=namespace_name,
        )
        owner_name = _object_name(definition.tenant_id, "runtime")
        owner = self._get_entity("users", owner_name, expected_name=owner_name)
        tenant_key = definition.tenant_id
        identity = definition.stable_external_key
        name = _object_name(tenant_key, identity)
        lookup_name = f"{namespace_name}.{name}"
        payload: dict[str, object] = {
            "name": name,
            "displayName": definition.name,
            "description": _description_with_metadata(
                definition.description,
                {
                    "catalog_product_definition": canonical_bytes(definition).decode("utf-8"),
                    "definition_digest": digest(definition),
                },
            ),
            "glossary": namespace_name,
            "owners": [{"id": owner.id, "type": "user"}],
        }
        try:
            entity = self._get_entity(
                "glossaryTerms",
                lookup_name,
                expected_name=name,
                expected_fqn=lookup_name,
                fields=("owners",),
            )
        except _ObjectNotFoundError:
            entity = self._ensure_entity(
                collection="glossaryTerms",
                name=name,
                lookup_name=lookup_name,
                payload=payload,
                fields=("owners",),
            )
        entity = self._validated_entity_relationships(
            collection="glossaryTerms", entity=entity, payload=payload
        )
        if not self._governed_glossary_term_payload_matches(entity=entity, payload=payload):
            raise CatalogProviderError(
                "OpenMetadata product snapshot conflicts with immutable authority",
                classification="conflict",
            )
        self._record_provider_entity_resource(collection="glossaryTerms", identifier=entity.id)
        self._ensure_native_product_entities(definition=definition, owner=owner)
        return self.get_product_catalog_snapshot(
            tenant_id=tenant_key, stable_external_key=definition.stable_external_key
        )

    def get_product_catalog_snapshot(
        self, *, tenant_id: str, stable_external_key: str
    ) -> CatalogProductObservation:
        name = _object_name(tenant_id, stable_external_key)
        namespace_name = _object_name(tenant_id, "namespace")
        entity = self._get_entity(
            "glossaryTerms",
            f"{namespace_name}.{name}",
            expected_name=name,
            expected_fqn=f"{namespace_name}.{name}",
            fields=("owners",),
        )
        entity = self._validated_entity_relationships(
            collection="glossaryTerms",
            entity=entity,
            payload={"glossary": namespace_name},
        )
        _, metadata = _description_metadata(
            entity.description,
            required=frozenset({"catalog_product_definition", "definition_digest"}),
        )
        try:
            definition = CatalogProductDefinition.model_validate_json(
                metadata["catalog_product_definition"], strict=True
            )
        except ValidationError:
            raise CatalogProviderError(
                "OpenMetadata product snapshot failed provider validation",
                classification="permanent",
            ) from None
        if (
            definition.tenant_id != tenant_id
            or definition.stable_external_key != stable_external_key
            or metadata["definition_digest"] != digest(definition)
        ):
            raise CatalogProviderError(
                "OpenMetadata product snapshot failed authority validation",
                classification="permanent",
            )
        self._observe_native_product_entities(definition=definition)
        return CatalogProductObservation(
            tenant_id=tenant_id,
            stable_external_key=stable_external_key,
            definition=definition,
            definition_digest=metadata["definition_digest"],
            provider_version=self.health().provider_version,
        )

    def ensure_native_table_snapshot(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        definition = CatalogNativeTableDefinition.model_validate(
            definition.model_dump(mode="python"), strict=True
        )
        hierarchy = definition.warehouse
        service_type = _native_database_service_type(hierarchy.warehouse_provider)
        service_fqn = hierarchy.database_service_name
        database_fqn = f"{service_fqn}.{hierarchy.database_name}"
        schema_fqn = f"{database_fqn}.{hierarchy.schema_name}"
        table_fqn = f"{schema_fqn}.{hierarchy.table_name}"

        service_payload = self._native_hierarchy_payload(
            definition=definition,
            level="database_service",
            name=hierarchy.database_service_name,
            display_name=f"PillarMesh managed {service_type} warehouse",
            parent_payload={"serviceType": service_type},
        )
        service, service_created = self._ensure_native_table_entity(
            collection="databaseServices",
            name=hierarchy.database_service_name,
            fqn=service_fqn,
            payload=service_payload,
        )
        self._record_created_native_resource(
            collection="databaseServices", entity=service, created=service_created
        )
        if not self._native_database_service_matches(
            entity=service, payload=service_payload, service_type=service_type
        ):
            raise CatalogProviderError(
                "OpenMetadata native database service conflicts with immutable authority",
                classification="conflict",
            )

        database_payload = self._native_hierarchy_payload(
            definition=definition,
            level="database",
            name=hierarchy.database_name,
            display_name=hierarchy.database_name,
            parent_payload={"service": service_fqn},
        )
        database, database_created = self._ensure_native_table_entity(
            collection="databases",
            name=hierarchy.database_name,
            fqn=database_fqn,
            payload=database_payload,
        )
        self._record_created_native_resource(
            collection="databases", entity=database, created=database_created
        )
        if not self._native_database_matches(
            entity=database, payload=database_payload, service_fqn=service_fqn
        ):
            raise CatalogProviderError(
                "OpenMetadata native database conflicts with immutable authority",
                classification="conflict",
            )

        schema_payload = self._native_hierarchy_payload(
            definition=definition,
            level="database_schema",
            name=hierarchy.schema_name,
            display_name=hierarchy.schema_name,
            parent_payload={"database": database_fqn},
        )
        schema, schema_created = self._ensure_native_table_entity(
            collection="databaseSchemas",
            name=hierarchy.schema_name,
            fqn=schema_fqn,
            payload=schema_payload,
        )
        self._record_created_native_resource(
            collection="databaseSchemas", entity=schema, created=schema_created
        )
        if not self._native_database_schema_matches(
            entity=schema, payload=schema_payload, database_fqn=database_fqn
        ):
            raise CatalogProviderError(
                "OpenMetadata native database schema conflicts with immutable authority",
                classification="conflict",
            )

        owner_name = _object_name(definition.product.tenant_id, "runtime")
        owner = self._get_entity("users", owner_name, expected_name=owner_name)
        owner_payload = [{"id": str(owner.id), "type": "user"}]
        table_payload = self._native_table_payload(
            definition=definition,
            schema_fqn=schema_fqn,
            owner_payload=owner_payload,
        )
        table, table_created = self._ensure_native_table_entity(
            collection="tables",
            name=hierarchy.table_name,
            fqn=table_fqn,
            payload=table_payload,
            fields=("columns", "databaseSchema", "dataProducts", "domains", "owners"),
        )
        self._record_created_native_resource(
            collection="tables", entity=table, created=table_created
        )
        if not self._native_table_matches(
            entity=table,
            payload=table_payload,
            schema_fqn=schema_fqn,
        ):
            raise CatalogProviderError(
                "OpenMetadata native table conflicts with immutable authority",
                classification="conflict",
            )
        return self.get_native_table_snapshot(definition)

    def get_native_table_snapshot(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        definition = CatalogNativeTableDefinition.model_validate(
            definition.model_dump(mode="python"), strict=True
        )
        hierarchy = definition.warehouse
        service_fqn = hierarchy.database_service_name
        database_fqn = f"{service_fqn}.{hierarchy.database_name}"
        schema_fqn = f"{database_fqn}.{hierarchy.schema_name}"
        table_fqn = f"{schema_fqn}.{hierarchy.table_name}"
        service_type = _native_database_service_type(hierarchy.warehouse_provider)

        service = self._get_entity(
            "databaseServices",
            service_fqn,
            expected_name=hierarchy.database_service_name,
            expected_fqn=service_fqn,
        )
        service_payload = self._native_hierarchy_payload(
            definition=definition,
            level="database_service",
            name=hierarchy.database_service_name,
            display_name=f"PillarMesh managed {service_type} warehouse",
            parent_payload={"serviceType": service_type},
        )
        database = self._get_entity(
            "databases",
            database_fqn,
            expected_name=hierarchy.database_name,
            expected_fqn=database_fqn,
        )
        database_payload = self._native_hierarchy_payload(
            definition=definition,
            level="database",
            name=hierarchy.database_name,
            display_name=hierarchy.database_name,
            parent_payload={"service": service_fqn},
        )
        schema = self._get_entity(
            "databaseSchemas",
            schema_fqn,
            expected_name=hierarchy.schema_name,
            expected_fqn=schema_fqn,
        )
        schema_payload = self._native_hierarchy_payload(
            definition=definition,
            level="database_schema",
            name=hierarchy.schema_name,
            display_name=hierarchy.schema_name,
            parent_payload={"database": database_fqn},
        )
        owner_name = _object_name(definition.product.tenant_id, "runtime")
        owner = self._get_entity("users", owner_name, expected_name=owner_name)
        owner_payload = [{"id": str(owner.id), "type": "user"}]
        table = self._get_entity(
            "tables",
            table_fqn,
            expected_name=hierarchy.table_name,
            expected_fqn=table_fqn,
            fields=("columns", "databaseSchema", "dataProducts", "domains", "owners"),
        )
        table_payload = self._native_table_payload(
            definition=definition,
            schema_fqn=schema_fqn,
            owner_payload=owner_payload,
        )
        if not (
            self._native_database_service_matches(
                entity=service, payload=service_payload, service_type=service_type
            )
            and self._native_database_matches(
                entity=database, payload=database_payload, service_fqn=service_fqn
            )
            and self._native_database_schema_matches(
                entity=schema, payload=schema_payload, database_fqn=database_fqn
            )
            and self._native_table_matches(
                entity=table, payload=table_payload, schema_fqn=schema_fqn
            )
        ):
            raise CatalogProviderError(
                "OpenMetadata native table hierarchy conflicts with immutable authority",
                classification="conflict",
            )
        _, metadata = _description_metadata(
            table.description,
            required=frozenset({"catalog_native_table_definition", "definition_digest"}),
        )
        try:
            observed_definition = CatalogNativeTableDefinition.model_validate_json(
                metadata["catalog_native_table_definition"], strict=True
            )
        except ValidationError:
            raise CatalogProviderError(
                "OpenMetadata native table failed provider validation",
                classification="permanent",
            ) from None
        if observed_definition != definition or metadata["definition_digest"] != digest(definition):
            raise CatalogProviderError(
                "OpenMetadata native table failed authority validation",
                classification="permanent",
            )
        return CatalogNativeTableObservation(
            definition=observed_definition,
            definition_digest=metadata["definition_digest"],
            table_fully_qualified_name=table_fqn,
            provider_version=self.health().provider_version,
        )

    @staticmethod
    def _native_hierarchy_payload(
        *,
        definition: CatalogNativeTableDefinition,
        level: Literal["database_service", "database", "database_schema"],
        name: str,
        display_name: str,
        parent_payload: Mapping[str, object],
    ) -> dict[str, object]:
        hierarchy = definition.warehouse
        authority: dict[str, object] = {
            "tenant_id": hierarchy.tenant_id,
            "warehouse_binding_id": hierarchy.warehouse_binding_id,
            "warehouse_binding_revision": hierarchy.warehouse_binding_revision,
            "warehouse_provider": hierarchy.warehouse_provider,
            "database_service_name": hierarchy.database_service_name,
        }
        if level in {"database", "database_schema"}:
            authority["database_name"] = hierarchy.database_name
        if level == "database_schema":
            authority["schema_name"] = hierarchy.schema_name
        authority_json = canonical_bytes(authority).decode("utf-8")
        return {
            "name": name,
            "displayName": display_name,
            "description": _description_with_metadata(
                f"PillarMesh managed {level.replace('_', ' ')}.",
                {
                    "hierarchy_authority": authority_json,
                    "hierarchy_authority_digest": digest(authority),
                },
            ),
            **parent_payload,
        }

    @staticmethod
    def _native_table_payload(
        *,
        definition: CatalogNativeTableDefinition,
        schema_fqn: str,
        owner_payload: list[dict[str, str]],
    ) -> dict[str, object]:
        product = definition.product
        domain_name = _object_name(product.tenant_id, "product-domain")
        product_name = _object_name(product.tenant_id, product.stable_external_key)
        product_fqn = product_name
        columns: list[dict[str, object]] = []
        for column in product.columns:
            data_type, data_type_display = _native_column_data_type(column.type_name)
            payload: dict[str, object] = {
                "name": column.name,
                "dataType": data_type,
                "dataTypeDisplay": data_type_display,
                "constraint": "NULL" if column.nullable else "NOT_NULL",
            }
            if column.description is not None:
                payload["description"] = column.description
            columns.append(payload)
        return {
            "name": definition.warehouse.table_name,
            "displayName": product.name,
            "description": _description_with_metadata(
                product.description,
                {
                    "catalog_native_table_definition": canonical_bytes(definition).decode("utf-8"),
                    "definition_digest": digest(definition),
                },
            ),
            "tableType": "View",
            "columns": columns,
            "databaseSchema": schema_fqn,
            "domains": [domain_name],
            "dataProducts": [product_fqn],
            "owners": owner_payload,
        }

    def _ensure_native_table_entity(
        self,
        *,
        collection: Literal["databaseServices", "databases", "databaseSchemas", "tables"],
        name: str,
        fqn: str,
        payload: Mapping[str, object],
        fields: tuple[str, ...] = (),
    ) -> tuple[_EntityResponse, bool]:
        try:
            return (
                self._get_entity(
                    collection,
                    fqn,
                    expected_name=name,
                    expected_fqn=fqn,
                    fields=fields,
                ),
                False,
            )
        except _ObjectNotFoundError:
            response = self._request(
                "PUT", f"/api/v1/{_entity_endpoint(collection)}", json_payload=dict(payload)
            )
        return (
            self._validated_entity_identity(
                self._entity(collection, response), expected_name=name, expected_fqn=fqn
            ),
            True,
        )

    def _record_created_native_resource(
        self, *, collection: str, entity: _EntityResponse, created: bool
    ) -> None:
        if created:
            self._record_provider_entity_resource(collection=collection, identifier=entity.id)

    @staticmethod
    def _native_database_service_matches(
        *, entity: _EntityResponse, payload: Mapping[str, object], service_type: str
    ) -> bool:
        return (
            isinstance(entity, _DatabaseServiceResponse)
            and entity.display_name == payload["displayName"]
            and entity.description == payload["description"]
            and entity.service_type == service_type
            and entity.connection is None
        )

    @staticmethod
    def _native_database_matches(
        *, entity: _EntityResponse, payload: Mapping[str, object], service_fqn: str
    ) -> bool:
        return (
            isinstance(entity, _DatabaseResponse)
            and entity.display_name == payload["displayName"]
            and entity.description == payload["description"]
            and entity.service.type == "databaseService"
            and entity.service.fully_qualified_name == service_fqn
        )

    @staticmethod
    def _native_database_schema_matches(
        *, entity: _EntityResponse, payload: Mapping[str, object], database_fqn: str
    ) -> bool:
        return (
            isinstance(entity, _DatabaseSchemaResponse)
            and entity.display_name == payload["displayName"]
            and entity.description == payload["description"]
            and entity.database.type == "database"
            and entity.database.fully_qualified_name == database_fqn
        )

    @classmethod
    def _native_table_matches(
        cls, *, entity: _EntityResponse, payload: Mapping[str, object], schema_fqn: str
    ) -> bool:
        if not isinstance(entity, _TableResponse):
            return False
        expected_columns = payload.get("columns")
        if not isinstance(expected_columns, list) or len(entity.columns) != len(expected_columns):
            return False
        observed_columns = [
            {
                key: value
                for key, value in {
                    "name": column.name,
                    "dataType": column.data_type,
                    "dataTypeDisplay": column.data_type_display,
                    "constraint": column.constraint,
                    "description": column.description,
                }.items()
                if value is not None
            }
            for column in entity.columns
        ]
        expected_domains = payload.get("domains")
        expected_products = payload.get("dataProducts")
        if (
            not isinstance(expected_domains, list)
            or not all(isinstance(item, str) for item in expected_domains)
            or not isinstance(expected_products, list)
            or not all(isinstance(item, str) for item in expected_products)
        ):
            return False
        return (
            entity.display_name == payload["displayName"]
            and entity.description == payload["description"]
            and entity.table_type == payload["tableType"]
            and observed_columns == expected_columns
            and entity.database_schema.type == "databaseSchema"
            and entity.database_schema.fully_qualified_name == schema_fqn
            and _entity_reference_fqns(entity.domains) == tuple(expected_domains)
            and _entity_reference_fqns(entity.data_products) == tuple(expected_products)
            and cls._entity_owners_match(entity=entity, expected_owners=payload["owners"])
        )

    def _observe_native_product_entities(self, *, definition: CatalogProductDefinition) -> None:
        tenant_key = definition.tenant_id
        domain_name = _object_name(tenant_key, "product-domain")
        owner_name = _object_name(tenant_key, "runtime")
        owner = self._get_entity("users", owner_name, expected_name=owner_name)
        owner_payload = [{"id": owner.id, "type": "user"}]
        domain_payload = self._native_domain_payload(
            tenant_key=tenant_key, owner_payload=owner_payload, include_name=False
        )
        domain = self._get_entity(
            "domains",
            domain_name,
            expected_name=domain_name,
            expected_fqn=domain_name,
            fields=("owners",),
        )
        if not self._native_domain_matches(domain=domain, payload=domain_payload):
            raise CatalogProviderError(
                "OpenMetadata native tenant domain conflicts with immutable authority",
                classification="conflict",
            )
        product_name = _object_name(tenant_key, definition.stable_external_key)
        product_payload = self._native_product_payload(
            definition=definition,
            domain_name=domain_name,
            owner_payload=owner_payload,
            include_identity=False,
        )
        product = self._get_entity(
            "dataProducts",
            product_name,
            expected_name=product_name,
            expected_fqn=product_name,
            fields=("owners", "domains"),
        )
        if not self._native_product_matches(
            product=product, payload=product_payload, domain_name=domain_name
        ):
            raise CatalogProviderError(
                "OpenMetadata native data product conflicts with immutable authority",
                classification="conflict",
            )

    def _ensure_native_product_entities(
        self, *, definition: CatalogProductDefinition, owner: _EntityResponse
    ) -> None:
        tenant_key = definition.tenant_id
        domain_name = _object_name(tenant_key, "product-domain")
        owner_payload = [{"id": owner.id, "type": "user"}]
        domain_payload = self._native_domain_payload(
            tenant_key=tenant_key, owner_payload=owner_payload, include_name=True
        )
        domain = self._ensure_immutable_native_entity(
            collection="domains",
            name=domain_name,
            lookup_name=domain_name,
            payload=domain_payload,
        )
        if not self._native_domain_matches(domain=domain, payload=domain_payload):
            raise CatalogProviderError(
                "OpenMetadata native tenant domain conflicts with immutable authority",
                classification="conflict",
            )
        self._record_provider_entity_resource(collection="domains", identifier=domain.id)

        product_name = _object_name(tenant_key, definition.stable_external_key)
        product_payload = self._native_product_payload(
            definition=definition,
            domain_name=domain_name,
            owner_payload=owner_payload,
            include_identity=True,
        )
        product = self._ensure_immutable_native_entity(
            collection="dataProducts",
            name=product_name,
            lookup_name=product_name,
            payload=product_payload,
        )
        if not self._native_product_matches(
            product=product, payload=product_payload, domain_name=domain_name
        ):
            raise CatalogProviderError(
                "OpenMetadata native data product conflicts with immutable authority",
                classification="conflict",
            )
        self._record_provider_entity_resource(collection="dataProducts", identifier=product.id)

    @staticmethod
    def _native_domain_payload(
        *, tenant_key: str, owner_payload: list[dict[str, str]], include_name: bool
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "displayName": f"PillarMesh {tenant_key} data products",
            "description": _description_with_metadata(
                "PillarMesh managed tenant data-product domain.",
                {
                    "tenant_key": tenant_key,
                    "tenant_domain_identity": digest(
                        {
                            "domain": "pillarmesh-openmetadata-product-domain-v1",
                            "tenant": tenant_key,
                        }
                    ),
                },
            ),
            "domainType": "Consumer-aligned",
            "owners": owner_payload,
        }
        if include_name:
            payload["name"] = _object_name(tenant_key, "product-domain")
        return payload

    @staticmethod
    def _native_product_payload(
        *,
        definition: CatalogProductDefinition,
        domain_name: str,
        owner_payload: list[dict[str, str]],
        include_identity: bool,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "displayName": definition.name,
            "description": _description_with_metadata(
                definition.description,
                {
                    "definition_digest": digest(definition),
                    "materialization_receipt_digest": definition.materialization_receipt_digest,
                    "snapshot_external_key": definition.stable_external_key,
                    "tenant_key": definition.tenant_id,
                },
            ),
            "owners": owner_payload,
            "dataProductType": "ANALYTIC_VIEW",
            "visibility": "PRIVATE",
        }
        if include_identity:
            payload["name"] = _object_name(definition.tenant_id, definition.stable_external_key)
            payload["domains"] = [domain_name]
        return payload

    def _ensure_immutable_native_entity(
        self,
        *,
        collection: Literal["domains", "dataProducts"],
        name: str,
        lookup_name: str,
        payload: Mapping[str, object],
    ) -> _EntityResponse:
        try:
            return self._get_entity(
                collection,
                lookup_name,
                expected_name=name,
                expected_fqn=lookup_name,
                fields=("owners", "domains") if collection == "dataProducts" else ("owners",),
            )
        except _ObjectNotFoundError:
            response = self._request("PUT", f"/api/v1/{collection}", json_payload=dict(payload))
        return self._validated_entity_identity(
            self._entity(collection, response),
            expected_name=name,
            expected_fqn=lookup_name,
        )

    @classmethod
    def _native_domain_matches(
        cls, *, domain: _EntityResponse, payload: Mapping[str, object]
    ) -> bool:
        return (
            isinstance(domain, _DomainResponse)
            and domain.display_name == payload["displayName"]
            and domain.description == payload["description"]
            and domain.domain_type == payload["domainType"]
            and cls._entity_owners_match(entity=domain, expected_owners=payload["owners"])
        )

    @classmethod
    def _native_product_matches(
        cls,
        *,
        product: _EntityResponse,
        payload: Mapping[str, object],
        domain_name: str,
    ) -> bool:
        return (
            isinstance(product, _DataProductResponse)
            and product.display_name == payload["displayName"]
            and product.description == payload["description"]
            and product.data_product_type == payload["dataProductType"]
            and product.visibility == payload["visibility"]
            and len(product.domains) == 1
            and cls._reference_matches_identity(
                product.domains[0], expected_name=domain_name, expected_type="domain"
            )
            and cls._entity_owners_match(entity=product, expected_owners=payload["owners"])
        )

    @staticmethod
    def _entity_owners_match(*, entity: _GovernedEntityResponse, expected_owners: object) -> bool:
        if not isinstance(expected_owners, list) or len(expected_owners) != 1:
            return False
        expected_owner = expected_owners[0]
        return (
            isinstance(expected_owner, dict)
            and entity.owners is not None
            and len(entity.owners) == 1
            and entity.owners[0].id == expected_owner.get("id")
            and entity.owners[0].type == expected_owner.get("type")
        )

    def ensure_classification(
        self,
        *,
        tenant_key: str,
        identity: str,
        payload: ClassificationPayload,
        idempotency_key: str,
    ) -> CatalogObjectRef:
        subject_identifier = self._resolve_glossary_term_identifier(
            tenant_key=tenant_key, reference=payload.subject_ref
        )
        name = _object_name(tenant_key, identity)
        entity = self._ensure_entity(
            collection="classifications",
            name=name,
            payload={
                "name": name,
                "description": _description_with_metadata(
                    "PillarMesh managed classification.",
                    {
                        "classification_ref": payload.classification_ref,
                        "provenance_ref": payload.provenance_ref,
                        "subject_ref": payload.subject_ref,
                    },
                ),
                "mutuallyExclusive": False,
            },
        )
        tag_name = _object_name(tenant_key, f"{identity}-tag")
        tag = self._ensure_tag(
            classification_name=entity.name,
            name=tag_name,
            description=payload.provenance_ref,
        )
        self._record_provider_entity_resource(collection="tags", identifier=tag.id)
        tag_fqn = f"{entity.name}.{tag.name}"
        subject = self._validated_classification_subject(
            response=self._get(f"/api/v1/glossaryTerms/{subject_identifier[0]}"),
            expected_identifier=subject_identifier[0],
        )
        if tag_fqn not in subject.tag_fqns:
            self._validated_classification_subject(
                response=self._request(
                    "PATCH",
                    f"/api/v1/glossaryTerms/{subject.id}",
                    json_payload=[
                        {
                            "op": "add",
                            "path": "/tags/-",
                            "value": {
                                "tagFQN": tag_fqn,
                                "source": "Classification",
                                "labelType": "Manual",
                                "state": "Confirmed",
                            },
                        }
                    ],
                    content_type="application/json-patch+json",
                ),
                expected_identifier=subject_identifier[0],
                expected_tag_fqn=tag_fqn,
            )
            self._validated_classification_subject(
                response=self._get(
                    f"/api/v1/glossaryTerms/{subject_identifier[0]}",
                    fields=("tags",),
                ),
                expected_identifier=subject_identifier[0],
                expected_tag_fqn=tag_fqn,
            )
        reference = _object_ref("classification", tenant_key, identity, entity)
        self._remember(reference, entity, entity_type="classification")
        return reference

    def ensure_lineage(
        self,
        *,
        tenant_key: str,
        identity: str,
        payload: LineagePayload,
        idempotency_key: str,
    ) -> CatalogObjectRef:
        name = _object_name(tenant_key, identity)
        from_identifier = self._resolve_glossary_term_identifier(
            tenant_key=tenant_key, reference=payload.from_ref
        )
        to_identifier = self._resolve_glossary_term_identifier(
            tenant_key=tenant_key, reference=payload.to_ref
        )
        lineage_description = _description_with_metadata(
            payload.evidence_ref,
            {"producer_ref": payload.producer_ref},
        )
        self._acknowledgement(
            self._request(
                "PUT",
                "/api/v1/lineage",
                json_payload={
                    "edge": {
                        "fromEntity": {"id": from_identifier[0], "type": from_identifier[1]},
                        "toEntity": {"id": to_identifier[0], "type": to_identifier[1]},
                        "lineageDetails": {
                            "description": lineage_description,
                        },
                    },
                },
            )
        )
        lineage_identifier = self._lineage_identifier(
            from_identifier=from_identifier,
            to_identifier=to_identifier,
            description=lineage_description,
        )
        self._record_provider_resource(
            collection="lineage",
            identifier=lineage_identifier,
        )
        self._verify_created_lineage(
            from_identifier=from_identifier,
            to_identifier=to_identifier,
            description=lineage_description,
        )
        stable_identity = _lineage_stable_identity(
            tenant_key=tenant_key,
            identity=identity,
            from_ref=payload.from_ref,
            to_ref=payload.to_ref,
        )
        reference = CatalogObjectRef(
            tenant_key=tenant_key,
            stable_identity=stable_identity,
            normalized_digest=digest({"identity": name, "payload": payload}),
        )
        self._remote_identifiers[_remote_identifier_key(reference)] = (
            lineage_identifier,
            "lineage",
        )
        return reference

    def get_object(self, *, tenant_key: str, identity: str) -> CatalogObjectSnapshot:
        expected_kind, identity = _stable_reference_kind_and_identity(
            tenant_key=tenant_key, identity=identity
        )
        if expected_kind == "lineage":
            return self._get_lineage_object(tenant_key=tenant_key, identity=identity)
        name = _object_name(tenant_key, identity)
        lookup_by_kind: dict[CatalogObjectKind, tuple[str, str]] = {
            "namespace": ("glossaries", name),
            "glossary_term": ("glossaryTerms", _glossary_term_fqn(tenant_key, identity)),
            "classification": ("classifications", name),
        }
        if expected_kind is None:
            object_lookups: tuple[tuple[str, str, CatalogObjectKind], ...] = (
                ("glossaries", name, "namespace"),
                ("glossaryTerms", _glossary_term_fqn(tenant_key, identity), "glossary_term"),
                ("classifications", name, "classification"),
            )
        else:
            collection, lookup_name = lookup_by_kind[expected_kind]
            object_lookups = ((collection, lookup_name, expected_kind),)
        for collection, lookup_name, object_kind in object_lookups:
            try:
                entity = self._get_entity(
                    collection,
                    lookup_name,
                    expected_name=name,
                    expected_fqn=lookup_name,
                )
            except _ObjectNotFoundError:
                continue
            if collection == "glossaryTerms":
                entity = self._validated_entity_relationships(
                    collection=collection,
                    entity=entity,
                    payload={"glossary": _object_name(tenant_key, "namespace")},
                )
            if object_kind == "namespace":
                normalized_payload: dict[str, str | tuple[str, ...]] = {
                    "name": tenant_key,
                    "namespace": tenant_key,
                }
            elif object_kind == "glossary_term":
                definition, metadata = _description_metadata(
                    entity.description,
                    required=frozenset({"owner_ref", "provenance_ref"}),
                )
                normalized_payload = {
                    "name": entity.display_name or "",
                    "definition": definition,
                    "owner_ref": metadata["owner_ref"],
                    "provenance_ref": metadata["provenance_ref"],
                }
            else:
                _, metadata = _description_metadata(
                    entity.description,
                    required=frozenset({"classification_ref", "provenance_ref", "subject_ref"}),
                )
                normalized_payload = {
                    "subject_ref": metadata["subject_ref"],
                    "classification_ref": metadata["classification_ref"],
                    "provenance_ref": metadata["provenance_ref"],
                }
            return CatalogObjectSnapshot(
                tenant_key=tenant_key,
                stable_identity=_stable_identity(object_kind, tenant_key, identity),
                logical_identity=identity,
                object_kind=object_kind,
                normalized_payload=normalized_payload,
                normalized_digest=digest(normalized_payload),
            )
        raise CatalogProviderError("OpenMetadata object was not found", classification="permanent")

    def _get_lineage_object(self, *, tenant_key: str, identity: str) -> CatalogObjectSnapshot:
        stable_reference = _decode_lineage_stable_reference(identity)
        stable_identity = _stable_identity("lineage", tenant_key, identity)
        from_identifier = self._resolve_glossary_term_identifier(
            tenant_key=tenant_key, reference=stable_reference.from_ref
        )
        to_identifier = self._resolve_glossary_term_identifier(
            tenant_key=tenant_key, reference=stable_reference.to_ref
        )
        response = self._get(
            f"/api/v1/lineage/getLineageEdge/{from_identifier[0]}/{to_identifier[0]}"
        )
        try:
            edge = _LineageEdgeResponse.model_validate(self._payload(response)).edge
        except ValidationError:
            raise CatalogProviderError(
                "OpenMetadata lineage response failed provider validation",
                classification="permanent",
            ) from None
        evidence_ref, metadata = _description_metadata(
            edge.description,
            required=frozenset({"producer_ref"}),
        )
        payload = {
            "from_ref": stable_reference.from_ref,
            "to_ref": stable_reference.to_ref,
            "producer_ref": metadata["producer_ref"],
            "evidence_ref": evidence_ref,
        }
        return CatalogObjectSnapshot(
            tenant_key=tenant_key,
            stable_identity=stable_identity,
            logical_identity=stable_reference.identity,
            object_kind="lineage",
            normalized_payload=payload,
            normalized_digest=digest(payload),
        )

    def delete_object(self, reference: CatalogObjectRef) -> None:
        remote_identifier = self._remote_identifiers.get(_remote_identifier_key(reference))
        if remote_identifier is None:
            raise CatalogProviderError(
                "OpenMetadata object identifier was not recorded",
                classification="invalid_request",
            )
        object_id, entity_type = remote_identifier
        collection = {
            "classification": "classifications",
            "glossary": "glossaries",
            "glossaryTerm": "glossaryTerms",
            "user": "users",
        }.get(entity_type)
        if collection is None:
            raise CatalogProviderError(
                "OpenMetadata object type cannot be deleted",
                classification="invalid_request",
            )
        response = self._request(
            "DELETE",
            f"/api/v1/{collection}/{object_id}?recursive=true&hardDelete=true",
        )
        self._validated_operation_entity(
            collection=collection,
            response=response,
            expected_identifier=object_id,
            failure_message="OpenMetadata delete response failed provider validation",
        )
        del self._remote_identifiers[_remote_identifier_key(reference)]
        self._recorded_provider_resources.pop((collection, object_id), None)

    def discovered_resources(self) -> tuple[_RecordedProviderResource, ...]:
        return tuple(self._recorded_provider_resources.values())

    def delete_recorded_resource(self, *, collection: str, identifier: str) -> None:
        if collection == "lineage":
            lineage = self._validated_lineage_identifier(identifier)
            with suppress(_ObjectNotFoundError):
                self._acknowledgement(
                    self._request(
                        "DELETE",
                        "/api/v1/lineage/"
                        f"{lineage.from_entity.type}/{lineage.from_entity.id}/"
                        f"{lineage.to_entity.type}/{lineage.to_entity.id}",
                    )
                )
            self._recorded_provider_resources.pop((collection, identifier), None)
            return
        if collection not in _ENTITY_RESPONSE_MODELS:
            raise CatalogProviderError(
                "OpenMetadata object type cannot be deleted",
                classification="invalid_request",
            )
        with suppress(_ObjectNotFoundError):
            response = self._request(
                "DELETE",
                f"/api/v1/{_entity_endpoint(collection)}/{identifier}"
                "?recursive=true&hardDelete=true",
            )
            self._validated_operation_entity(
                collection=collection,
                response=response,
                expected_identifier=identifier,
                failure_message="OpenMetadata delete response failed provider validation",
            )
        self._recorded_provider_resources.pop((collection, identifier), None)

    def recorded_resource_is_absent(self, *, collection: str, identifier: str) -> bool:
        if collection == "lineage":
            lineage = self._validated_lineage_identifier(identifier)
            try:
                response = self._get(
                    "/api/v1/lineage/getLineageEdge/"
                    f"{lineage.from_entity.id}/{lineage.to_entity.id}"
                )
            except _ObjectNotFoundError:
                return True
            edge_response: _LineageEdgeResponse | None
            try:
                edge_response = _LineageEdgeResponse.model_validate(self._payload(response))
            except ValidationError:
                edge_response = None
            if edge_response is None or edge_response.edge.description != lineage.description:
                raise CatalogProviderError(
                    "OpenMetadata lineage verification response failed provider validation",
                    classification="permanent",
                )
            return False
        if collection not in _ENTITY_RESPONSE_MODELS:
            raise CatalogProviderError(
                "OpenMetadata object type cannot be verified",
                classification="invalid_request",
            )
        try:
            response = self._get(f"/api/v1/{_entity_endpoint(collection)}/{identifier}")
        except _ObjectNotFoundError:
            return True
        self._validated_operation_entity(
            collection=collection,
            response=response,
            expected_identifier=identifier,
            failure_message="OpenMetadata verification response failed provider validation",
        )
        return False

    def assign_owner(self, reference: CatalogObjectRef, owner: CatalogObjectRef) -> None:
        if reference.tenant_key != owner.tenant_key:
            raise CatalogProviderError(
                "OpenMetadata owner assignment references must belong to the same tenant",
                classification="invalid_request",
            )
        remote_identifier = self._remote_identifiers.get(_remote_identifier_key(reference))
        owner_identifier = self._remote_identifiers.get(_remote_identifier_key(owner))
        if remote_identifier is None or owner_identifier is None or owner_identifier[1] != "user":
            raise CatalogProviderError(
                "OpenMetadata owner assignment references were not recorded",
                classification="invalid_request",
            )
        collection = {
            "glossary": "glossaries",
            "glossaryTerm": "glossaryTerms",
        }.get(remote_identifier[1])
        if collection is None:
            raise CatalogProviderError(
                "OpenMetadata object type cannot receive an owner",
                classification="invalid_request",
            )
        response = self._request(
            "PATCH",
            f"/api/v1/{collection}/{remote_identifier[0]}",
            json_payload=[
                {
                    "op": "add",
                    "path": "/owners",
                    "value": [{"id": owner_identifier[0], "type": "user"}],
                }
            ],
            content_type="application/json-patch+json",
        )
        entity = self._validated_operation_entity(
            collection=collection,
            response=response,
            expected_identifier=remote_identifier[0],
            failure_message="OpenMetadata owner response failed provider validation",
        )
        if not isinstance(entity, _GovernedEntityResponse) or not any(
            assigned_owner.id == owner_identifier[0] and assigned_owner.type == "user"
            for assigned_owner in entity.owners or ()
        ):
            raise CatalogProviderError(
                "OpenMetadata owner response failed provider validation",
                classification="permanent",
            )

    def assert_administration_denied(self) -> None:
        try:
            self._get("/api/v1/users/generateRandomPwd")
        except CatalogProviderError as error:
            if error.classification in {"authentication", "authorization"}:
                return
            raise
        raise CatalogProviderError(
            "runtime identity was granted OpenMetadata administration", classification="permanent"
        )

    def assert_other_tenant_namespace_denied(self, *, tenant_key: str) -> None:
        try:
            namespace_name = _object_name(tenant_key, "namespace")
            self._get_entity(
                "glossaries",
                namespace_name,
                expected_name=namespace_name,
                expected_fqn=namespace_name,
            )
        except CatalogProviderError as error:
            if error.classification == "authorization":
                return
            raise
        raise CatalogProviderError(
            "runtime identity resolved another tenant namespace", classification="permanent"
        )

    def _ensure_entity(
        self,
        *,
        collection: str,
        name: str,
        payload: Mapping[str, object],
        lookup_name: str | None = None,
        fields: tuple[str, ...] = (),
    ) -> _EntityResponse:
        try:
            expected_fqn = lookup_name or name
            entity = self._get_entity(
                collection,
                expected_fqn,
                expected_name=name,
                expected_fqn=expected_fqn,
                fields=fields,
            )
        except _ObjectNotFoundError:
            pass
        else:
            return self._validated_entity_relationships(
                collection=collection,
                entity=entity,
                payload=payload,
            )
        try:
            created_entity = self._entity(
                collection, self._request("POST", f"/api/v1/{collection}", json_payload=payload)
            )
        except CatalogProviderError as error:
            if error.classification != "conflict":
                raise
            try:
                entity = self._get_entity(
                    collection,
                    expected_fqn,
                    expected_name=name,
                    expected_fqn=expected_fqn,
                    fields=fields,
                )
            except _ObjectNotFoundError:
                raise error from None
            return self._validated_entity_relationships(
                collection=collection,
                entity=entity,
                payload=payload,
            )
        identified_entity = self._validated_entity_identity(
            created_entity,
            expected_name=name,
            expected_fqn=lookup_name or name,
        )
        return self._validated_entity_relationships(
            collection=collection,
            entity=identified_entity,
            payload=payload,
        )

    def _ensure_governed_glossary_term(
        self,
        *,
        name: str,
        lookup_name: str,
        payload: Mapping[str, object],
    ) -> _EntityResponse:
        entity: _EntityResponse | None
        try:
            entity = self._get_entity(
                "glossaryTerms",
                lookup_name,
                expected_name=name,
                expected_fqn=lookup_name,
                fields=("owners",),
            )
        except _ObjectNotFoundError:
            entity = None
        if entity is None:
            return self._ensure_entity(
                collection="glossaryTerms",
                name=name,
                lookup_name=lookup_name,
                payload=payload,
                fields=("owners",),
            )

        entity = self._validated_entity_relationships(
            collection="glossaryTerms",
            entity=entity,
            payload=payload,
        )
        patch = self._governed_glossary_term_patch(entity=entity, payload=payload)
        if not patch:
            return entity

        try:
            response = self._request(
                "PATCH",
                f"/api/v1/glossaryTerms/{entity.id}",
                json_payload=patch,
                content_type="application/json-patch+json",
            )
        except CatalogProviderError as error:
            if error.classification != "conflict":
                raise
            readback = self._read_governed_glossary_term(
                name=name,
                lookup_name=lookup_name,
                payload=payload,
            )
            if not self._governed_glossary_term_payload_matches(entity=readback, payload=payload):
                raise error from None
            return readback

        updated = self._validated_operation_entity(
            collection="glossaryTerms",
            response=response,
            expected_identifier=entity.id,
            failure_message="OpenMetadata glossary term update response failed provider validation",
        )
        updated = self._validated_entity_relationships(
            collection="glossaryTerms",
            entity=updated,
            payload=payload,
        )
        if not self._governed_glossary_term_payload_matches(entity=updated, payload=payload):
            raise CatalogProviderError(
                "OpenMetadata glossary term update response failed provider validation",
                classification="permanent",
            )

        readback = self._read_governed_glossary_term(
            name=name,
            lookup_name=lookup_name,
            payload=payload,
        )
        if not self._governed_glossary_term_payload_matches(entity=readback, payload=payload):
            raise CatalogProviderError(
                "OpenMetadata glossary term verification response failed provider validation",
                classification="permanent",
            )
        return readback

    def _read_governed_glossary_term(
        self,
        *,
        name: str,
        lookup_name: str,
        payload: Mapping[str, object],
    ) -> _EntityResponse:
        entity = self._get_entity(
            "glossaryTerms",
            lookup_name,
            expected_name=name,
            expected_fqn=lookup_name,
            fields=("owners",),
        )
        return self._validated_entity_relationships(
            collection="glossaryTerms",
            entity=entity,
            payload=payload,
        )

    @classmethod
    def _governed_glossary_term_patch(
        cls,
        *,
        entity: _EntityResponse,
        payload: Mapping[str, object],
    ) -> list[dict[str, object]]:
        if not isinstance(entity, _GlossaryTermResponse):
            raise CatalogProviderError(
                "OpenMetadata glossary term response failed provider validation",
                classification="permanent",
            )
        patch: list[dict[str, object]] = []
        for path, current_value, expected_value in (
            ("/displayName", entity.display_name, payload["displayName"]),
            ("/description", entity.description, payload["description"]),
        ):
            if current_value != expected_value:
                patch.append({"op": "replace", "path": path, "value": expected_value})

        expected_owners = payload["owners"]
        if not cls._governed_glossary_term_owners_match(
            entity=entity, expected_owners=expected_owners
        ):
            patch.append({"op": "add", "path": "/owners", "value": expected_owners})

        return patch

    @classmethod
    def _governed_glossary_term_payload_matches(
        cls,
        *,
        entity: _EntityResponse,
        payload: Mapping[str, object],
    ) -> bool:
        if not isinstance(entity, _GlossaryTermResponse):
            return False
        return (
            entity.display_name == payload["displayName"]
            and entity.description == payload["description"]
            and cls._governed_glossary_term_owners_match(
                entity=entity, expected_owners=payload["owners"]
            )
        )

    @staticmethod
    def _governed_glossary_term_owners_match(
        *,
        entity: _GlossaryTermResponse,
        expected_owners: object,
    ) -> bool:
        if not isinstance(expected_owners, list) or len(expected_owners) != 1:
            return False
        expected_owner = expected_owners[0]
        return (
            isinstance(expected_owner, dict)
            and entity.owners is not None
            and len(entity.owners) == 1
            and entity.owners[0].id == expected_owner.get("id")
            and entity.owners[0].type == expected_owner.get("type")
        )

    def _ensure_tag(
        self, *, classification_name: str, name: str, description: str
    ) -> _EntityResponse:
        fully_qualified_name = f"{classification_name}.{name}"
        try:
            entity = self._get_entity(
                "tags",
                fully_qualified_name,
                expected_name=name,
                expected_fqn=fully_qualified_name,
            )
        except _ObjectNotFoundError:
            pass
        else:
            return self._validated_tag_relationships(
                entity,
                expected_classification=classification_name,
            )
        try:
            created_entity = self._entity(
                "tags",
                self._request(
                    "POST",
                    "/api/v1/tags",
                    json_payload={
                        "name": name,
                        "description": description,
                        "classification": classification_name,
                    },
                ),
            )
        except CatalogProviderError as error:
            if error.classification != "conflict":
                raise
            try:
                entity = self._get_entity(
                    "tags",
                    fully_qualified_name,
                    expected_name=name,
                    expected_fqn=fully_qualified_name,
                )
            except _ObjectNotFoundError:
                raise error from None
            return self._validated_tag_relationships(
                entity,
                expected_classification=classification_name,
            )
        identified_entity = self._validated_entity_identity(
            created_entity,
            expected_name=name,
            expected_fqn=fully_qualified_name,
        )
        return self._validated_tag_relationships(
            identified_entity,
            expected_classification=classification_name,
        )

    def _ensure_runtime_role(self, tenant_key: str) -> _EntityResponse:
        policy_name = _object_name(tenant_key, "runtime-deny-policy")
        policy = self._ensure_entity(
            collection="policies",
            name=policy_name,
            payload={
                "name": policy_name,
                "description": "Restrict the managed runtime identity to owned resources.",
                "rules": [
                    {
                        "name": "deny-unowned-resources",
                        "description": "Deny runtime access to resources it does not own.",
                        "resources": ["all"],
                        "operations": ["All"],
                        "effect": "deny",
                        "condition": "!isOwner()",
                    }
                ],
                "enabled": True,
            },
        )
        self._record_provider_entity_resource(collection="policies", identifier=policy.id)
        role_name = _object_name(tenant_key, "runtime-role")
        role = self._ensure_entity(
            collection="roles",
            name=role_name,
            payload={
                "name": role_name,
                "description": "PillarMesh managed runtime role.",
                "policies": [policy.name],
            },
        )
        self._record_provider_entity_resource(collection="roles", identifier=role.id)
        return role

    def _get_entity(
        self,
        collection: str,
        name: str,
        *,
        expected_name: str,
        expected_fqn: str | None = None,
        fields: tuple[str, ...] = (),
    ) -> _EntityResponse:
        entity = self._entity(
            collection,
            self._get(f"/api/v1/{_entity_endpoint(collection)}/name/{name}", fields=fields),
        )
        return self._validated_entity_identity(
            entity,
            expected_name=expected_name,
            expected_fqn=expected_fqn,
        )

    def _resolve_glossary_term_identifier(
        self, *, tenant_key: str, reference: str
    ) -> tuple[str, str]:
        object_kind, identity = _stable_reference_kind_and_identity(
            tenant_key=tenant_key, identity=reference
        )
        if object_kind is not None and object_kind != "glossary_term":
            raise CatalogProviderError(
                "OpenMetadata reference is not a glossary term",
                classification="invalid_request",
            )
        stable_identity = _stable_identity("glossary_term", tenant_key, identity)
        cached_identifier = self._remote_identifiers.get((tenant_key, stable_identity))
        if cached_identifier is not None and cached_identifier[1] == "glossaryTerm":
            return cached_identifier
        name = _object_name(tenant_key, identity)
        namespace_name = _object_name(tenant_key, "namespace")
        entity = self._get_entity(
            "glossaryTerms",
            f"{namespace_name}.{name}",
            expected_name=name,
            expected_fqn=f"{namespace_name}.{name}",
        )
        entity = self._validated_entity_relationships(
            collection="glossaryTerms",
            entity=entity,
            payload={"glossary": namespace_name},
        )
        identifier = (entity.id, "glossaryTerm")
        self._remote_identifiers[(tenant_key, stable_identity)] = identifier
        return identifier

    @staticmethod
    def _validated_entity_identity(
        entity: _EntityResponse,
        *,
        expected_name: str,
        expected_fqn: str | None,
    ) -> _EntityResponse:
        if entity.name != expected_name or (
            entity.fully_qualified_name is not None
            and expected_fqn is not None
            and entity.fully_qualified_name != expected_fqn
        ):
            raise CatalogProviderError(
                "OpenMetadata response failed entity identity validation",
                classification="permanent",
            )
        return entity

    @classmethod
    def _validated_entity_relationships(
        cls,
        *,
        collection: str,
        entity: _EntityResponse,
        payload: Mapping[str, object],
    ) -> _EntityResponse:
        if collection != "glossaryTerms":
            return entity
        expected_glossary = payload.get("glossary")
        expected_parent = payload.get("parent")
        parent_matches = (
            entity.parent is None
            if isinstance(entity, _GlossaryTermResponse) and expected_parent is None
            else isinstance(expected_parent, str)
            and isinstance(entity, _GlossaryTermResponse)
            and cls._reference_matches_identity(
                entity.parent,
                expected_name=expected_parent,
                expected_type="glossaryTerm",
            )
        )
        if (
            not isinstance(entity, _GlossaryTermResponse)
            or not isinstance(expected_glossary, str)
            or not cls._reference_matches_identity(
                entity.glossary,
                expected_name=expected_glossary,
                expected_type="glossary",
            )
            or not parent_matches
        ):
            raise CatalogProviderError(
                "OpenMetadata response failed entity relationship validation",
                classification="permanent",
            )
        return entity

    @staticmethod
    def _reference_matches_identity(
        reference: _EntityReference | None,
        *,
        expected_name: str,
        expected_type: str,
    ) -> bool:
        if reference is None or reference.type != expected_type:
            return False
        names = tuple(
            name for name in (reference.name, reference.fully_qualified_name) if name is not None
        )
        return bool(names) and all(name == expected_name for name in names)

    @staticmethod
    def _validated_classification_subject(
        *,
        response: httpx.Response,
        expected_identifier: str,
        expected_tag_fqn: str | None = None,
    ) -> _GlossaryTermResponse:
        subject: _EntityResponse | None
        try:
            subject = OpenMetadataClient._entity("glossaryTerms", response)
        except CatalogProviderError:
            subject = None
        if (
            not isinstance(subject, _GlossaryTermResponse)
            or subject.id != expected_identifier
            or (expected_tag_fqn is not None and expected_tag_fqn not in subject.tag_fqns)
        ):
            raise CatalogProviderError(
                "OpenMetadata classification response failed provider validation",
                classification="permanent",
            )
        return subject

    @classmethod
    def _validated_tag_relationships(
        cls,
        entity: _EntityResponse,
        *,
        expected_classification: str,
    ) -> _TagResponse:
        if (
            not isinstance(entity, _TagResponse)
            or not cls._reference_matches_identity(
                entity.classification,
                expected_name=expected_classification,
                expected_type="classification",
            )
            or entity.parent is not None
        ):
            raise CatalogProviderError(
                "OpenMetadata response failed entity relationship validation",
                classification="permanent",
            )
        return entity

    def _verify_created_lineage(
        self,
        *,
        from_identifier: tuple[str, str],
        to_identifier: tuple[str, str],
        description: str,
    ) -> None:
        exact_edge: _LineageEdgeResponse | None = None
        try:
            exact_response = self._get(
                f"/api/v1/lineage/getLineageEdge/{from_identifier[0]}/{to_identifier[0]}"
            )
        except _ObjectNotFoundError:
            pass
        else:
            try:
                exact_edge = _LineageEdgeResponse.model_validate(self._payload(exact_response))
            except ValidationError:
                exact_edge = None
        if exact_edge is None:
            raise CatalogProviderError(
                "OpenMetadata lineage verification response failed provider validation",
                classification="permanent",
            )

        graph: _EntityLineageResponse | None = None
        try:
            graph_response = self._get(
                f"/api/v1/lineage/{from_identifier[1]}/{from_identifier[0]}"
                "?upstreamDepth=0&downstreamDepth=1"
            )
        except _ObjectNotFoundError:
            pass
        else:
            try:
                graph = _EntityLineageResponse.model_validate(self._payload(graph_response))
            except ValidationError:
                graph = None
        graph_matches = (
            graph is not None
            and graph.entity.id == from_identifier[0]
            and graph.entity.type == from_identifier[1]
            and any(
                node.id == to_identifier[0] and node.type == to_identifier[1]
                for node in graph.nodes or ()
            )
            and any(
                edge.from_entity == from_identifier[0]
                and edge.to_entity == to_identifier[0]
                and edge.observed_description == description
                for edge in graph.downstream_edges or ()
            )
        )
        if not graph_matches:
            raise CatalogProviderError(
                "OpenMetadata lineage verification response failed provider validation",
                classification="permanent",
            )

    def _get(self, path: str, *, fields: tuple[str, ...] = ()) -> httpx.Response:
        headers = self._headers()
        response: httpx.Response | None
        try:
            if fields:
                response = self._transport.get(
                    self._url(path),
                    headers=headers,
                    params={"fields": ",".join(fields)},
                )
            else:
                response = self._transport.get(self._url(path), headers=headers)
        except Exception:
            response = None
        if response is None:
            raise CatalogProviderError(
                "OpenMetadata transport request failed", classification="transient"
            )
        return self._checked_response(response)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: object | None = None,
        content_type: str | None = None,
    ) -> httpx.Response:
        headers = self._headers()
        if content_type is not None:
            headers["Content-Type"] = content_type
        response: httpx.Response | None
        try:
            response = self._transport.request(
                method,
                self._url(path),
                headers=headers,
                json=json_payload,
            )
        except Exception:
            response = None
        if response is None:
            raise CatalogProviderError(
                "OpenMetadata transport request failed", classification="transient"
            )
        return self._checked_response(response)

    @staticmethod
    def _entity(collection: str, response: httpx.Response) -> _EntityResponse:
        entity_model = _ENTITY_RESPONSE_MODELS.get(collection)
        if entity_model is None:
            raise CatalogProviderError(
                "OpenMetadata entity endpoint is unsupported", classification="invalid_request"
            )
        entity: _EntityResponse | None
        try:
            entity = entity_model.model_validate(OpenMetadataClient._payload(response))
        except ValidationError:
            entity = None
        if entity is None:
            raise CatalogProviderError(
                "OpenMetadata response failed provider validation", classification="permanent"
            )
        return entity

    @staticmethod
    def _payload(response: httpx.Response) -> dict[str, object]:
        payload: object = None
        decoding_failed = False
        try:
            payload = response.json()
        except Exception:
            decoding_failed = True
        if decoding_failed:
            raise CatalogProviderError(
                "OpenMetadata response is not valid JSON", classification="permanent"
            )
        if not isinstance(payload, dict):
            raise CatalogProviderError(
                "OpenMetadata response has an invalid shape", classification="permanent"
            )
        normalized_payload: dict[str, object] = {}
        for key, value in payload.items():
            if not isinstance(key, str):
                raise CatalogProviderError(
                    "OpenMetadata response has an invalid shape", classification="permanent"
                )
            normalized_payload[key] = value
        return normalized_payload

    @staticmethod
    def _checked_response(response: httpx.Response) -> httpx.Response:
        if response.is_success:
            return response
        if response.status_code == 404:
            raise _ObjectNotFoundError(
                "OpenMetadata object was not found", classification="permanent"
            )
        raise CatalogProviderError(
            "OpenMetadata request was rejected",
            classification=_classification(response.status_code),
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self._access_token()}

    def _access_token(self) -> str:
        if self._bearer_token is not None:
            return self._bearer_token
        if self._credentials is None:
            raise CatalogProviderError(
                "OpenMetadata private credentials are unavailable", classification="authentication"
            )
        response: httpx.Response | None
        try:
            response = self._transport.request(
                "POST",
                self._url("/api/v1/users/login"),
                headers={},
                json={
                    "email": self._credentials.username,
                    "password": base64.b64encode(
                        self._credentials.password.get_secret_value().encode()
                    ).decode(),
                },
            )
        except Exception:
            response = None
        if response is None:
            raise CatalogProviderError(
                "OpenMetadata authentication request failed", classification="transient"
            )
        login: _LoginResponse | None
        try:
            login = _LoginResponse.model_validate(self._payload(self._checked_response(response)))
        except ValidationError:
            login = None
        if login is None:
            raise CatalogProviderError(
                "OpenMetadata authentication response failed provider validation",
                classification="permanent",
            )
        self._bearer_token = login.access_token
        return login.access_token

    def _url(self, path: str) -> str:
        return self._settings.base_url + path

    def _remember(
        self, reference: CatalogObjectRef, entity: _EntityResponse, *, entity_type: str
    ) -> None:
        self._remote_identifiers[_remote_identifier_key(reference)] = (entity.id, entity_type)
        resource_kind: Literal["namespace", "user", "object"]
        if entity_type == "glossary":
            resource_kind = "namespace"
        elif entity_type == "user":
            resource_kind = "user"
        else:
            resource_kind = "object"
        collection = {
            "classification": "classifications",
            "glossary": "glossaries",
            "glossaryTerm": "glossaryTerms",
            "user": "users",
        }.get(entity_type)
        if collection is None:
            raise CatalogProviderError(
                "OpenMetadata object type cannot be recorded",
                classification="invalid_request",
            )
        resource = _RecordedProviderResource(
            resource_kind=resource_kind,
            identifier=entity.id,
            collection=collection,
        )
        self._recorded_provider_resources[(collection, entity.id)] = resource

    def _record_provider_resource(self, *, collection: str, identifier: str) -> None:
        self._recorded_provider_resources[(collection, identifier)] = _RecordedProviderResource(
            resource_kind="object",
            identifier=identifier,
            collection=collection,
        )

    def _record_provider_entity_resource(self, *, collection: str, identifier: str) -> None:
        canonical_identifier: str | None
        try:
            canonical_identifier = str(UUID(identifier))
        except (TypeError, ValueError):
            canonical_identifier = None
        if canonical_identifier is None or canonical_identifier != identifier:
            raise CatalogProviderError(
                "OpenMetadata resource identifier failed provider validation",
                classification="permanent",
            )
        self._record_provider_resource(collection=collection, identifier=canonical_identifier)

    @staticmethod
    def _lineage_identifier(
        *,
        from_identifier: tuple[str, str],
        to_identifier: tuple[str, str],
        description: str,
    ) -> str:
        identifier: _LineageResourceIdentifier | None
        try:
            identifier = _LineageResourceIdentifier(
                fromEntity=_LineageEndpointIdentifier(
                    id=UUID(from_identifier[0]),
                    type=from_identifier[1],
                ),
                toEntity=_LineageEndpointIdentifier(
                    id=UUID(to_identifier[0]),
                    type=to_identifier[1],
                ),
                description=description,
            )
        except (TypeError, ValueError, ValidationError):
            identifier = None
        if identifier is None:
            raise CatalogProviderError(
                "OpenMetadata lineage identifier failed provider validation",
                classification="permanent",
            )
        return identifier.model_dump_json(by_alias=True)

    @staticmethod
    def _validated_lineage_identifier(identifier: str) -> _LineageResourceIdentifier:
        parsed: _LineageResourceIdentifier | None
        try:
            parsed = _LineageResourceIdentifier.model_validate_json(identifier)
        except (TypeError, ValueError, ValidationError):
            parsed = None
        if parsed is None or parsed.model_dump_json(by_alias=True) != identifier:
            raise CatalogProviderError(
                "OpenMetadata lineage identifier failed provider validation",
                classification="invalid_request",
            )
        return parsed

    def _service_identity_password(self, identity: str) -> str:
        if self._credentials is None:
            raise CatalogProviderError(
                "OpenMetadata private credentials are unavailable", classification="authentication"
            )
        return self._credentials.password_for_service_identity(identity).get_secret_value()

    @staticmethod
    def _validated_operation_entity(
        *,
        collection: str,
        response: httpx.Response,
        expected_identifier: str,
        failure_message: str,
    ) -> _EntityResponse:
        if response.status_code != 200:
            raise CatalogProviderError(failure_message, classification="permanent")
        entity: _EntityResponse | None
        try:
            entity = OpenMetadataClient._entity(collection, response)
        except CatalogProviderError:
            entity = None
        if entity is None:
            raise CatalogProviderError(failure_message, classification="permanent")
        if entity.id != expected_identifier:
            raise CatalogProviderError(failure_message, classification="permanent")
        return entity

    @staticmethod
    def _acknowledgement(response: httpx.Response) -> None:
        if response.status_code == 200 and not response.content:
            return
        raise CatalogProviderError(
            "OpenMetadata lineage response failed provider validation", classification="permanent"
        )

    @staticmethod
    def _password_change_acknowledgement(response: httpx.Response) -> None:
        if response.text == "Password Updated Successfully":
            return
        raise CatalogProviderError(
            "OpenMetadata password change response failed provider validation",
            classification="permanent",
        )


_NATIVE_COLUMN_DATA_TYPES = frozenset(
    {
        "NUMBER",
        "TINYINT",
        "SMALLINT",
        "INT",
        "BIGINT",
        "BYTEINT",
        "BYTES",
        "FLOAT",
        "DOUBLE",
        "DECIMAL",
        "NUMERIC",
        "TIMESTAMP",
        "TIMESTAMPZ",
        "TIME",
        "DATE",
        "DATETIME",
        "INTERVAL",
        "STRING",
        "MEDIUMTEXT",
        "TEXT",
        "CHAR",
        "LONG",
        "VARCHAR",
        "BOOLEAN",
        "BINARY",
        "VARBINARY",
        "ARRAY",
        "BLOB",
        "MAP",
        "STRUCT",
        "UNION",
        "SET",
        "GEOGRAPHY",
        "ENUM",
        "JSON",
        "UUID",
        "VARIANT",
        "GEOMETRY",
        "BYTEA",
        "XML",
        "UNKNOWN",
        "CIDR",
        "INET",
        "IPV4",
        "IPV6",
    }
)


def _entity_endpoint(collection: str) -> str:
    if collection == "databaseServices":
        return "services/databaseServices"
    return collection


def _native_database_service_type(
    warehouse_provider: Literal["postgresql", "clickhouse"],
) -> Literal["Postgres", "Clickhouse"]:
    if warehouse_provider == "postgresql":
        return "Postgres"
    return "Clickhouse"


def _native_column_data_type(type_name: str) -> tuple[str, str]:
    normalized = type_name.upper()
    if normalized in _NATIVE_COLUMN_DATA_TYPES:
        return normalized, normalized.lower()
    return "UNKNOWN", type_name


def _entity_reference_fqns(
    references: tuple[_EntityReference, ...] | None,
) -> tuple[str, ...]:
    if references is None or any(
        reference.fully_qualified_name is None for reference in references
    ):
        return ()
    return tuple(
        reference.fully_qualified_name
        for reference in references
        if reference.fully_qualified_name is not None
    )


def _description_with_metadata(description: str, metadata: Mapping[str, str]) -> str:
    if (
        not description
        or not metadata
        or any(not key or not value for key, value in metadata.items())
    ):
        raise CatalogProviderError(
            "OpenMetadata governed description metadata is invalid",
            classification="invalid_request",
        )
    encoded = base64.urlsafe_b64encode(canonical_bytes(dict(metadata))).decode("ascii").rstrip("=")
    return description + _METADATA_MARKER + encoded + _METADATA_SUFFIX


def _description_metadata(
    description: str | None, *, required: frozenset[str]
) -> tuple[str, dict[str, str]]:
    if description is None or not description.endswith(_METADATA_SUFFIX):
        raise CatalogProviderError(
            "OpenMetadata governed description metadata is unavailable",
            classification="permanent",
        )
    visible, marker, encoded = description.rpartition(_METADATA_MARKER)
    encoded = encoded.removesuffix(_METADATA_SUFFIX)
    parsed: JsonValue | None = None
    decoded = b""
    if marker and visible and encoded:
        try:
            decoded = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
            parsed = json.loads(decoded)
        except (UnicodeDecodeError, ValueError):
            parsed = None
    if (
        not isinstance(parsed, dict)
        or set(parsed) != required
        or any(not isinstance(value, str) or not value for value in parsed.values())
        or canonical_bytes(parsed) != decoded
    ):
        raise CatalogProviderError(
            "OpenMetadata governed description metadata failed provider validation",
            classification="permanent",
        )
    return visible, {key: value for key, value in parsed.items() if isinstance(value, str)}


def _classification(status_code: int) -> CatalogFailureClassification:
    if status_code == 429:
        return "throttled"
    if status_code == 401:
        return "authentication"
    if status_code == 403:
        return "authorization"
    if status_code == 409:
        return "conflict"
    if status_code in {400, 422}:
        return "invalid_request"
    if status_code >= 500:
        return "transient"
    return "permanent"


def _object_name(tenant_key: str, identity: str) -> str:
    return (
        "pm-"
        + digest(
            {
                "domain": "pillarmesh-openmetadata-v1",
                "tenant_key": tenant_key,
                "identity": identity,
            }
        )[:24]
    )


def _service_identity_username(tenant_key: str, identity: str) -> str:
    return _object_name(tenant_key, identity) + "@open-metadata.invalid"


def _glossary_term_fqn(tenant_key: str, identity: str) -> str:
    return f"{_object_name(tenant_key, 'namespace')}.{_object_name(tenant_key, identity)}"


def _stable_identity(kind: str, tenant_key: str, identity: str) -> str:
    return f"{kind}:{tenant_key}:{identity}"


def _lineage_stable_identity(*, tenant_key: str, identity: str, from_ref: str, to_ref: str) -> str:
    descriptor = _LineageStableReference(identity=identity, from_ref=from_ref, to_ref=to_ref)
    encoded_descriptor = (
        base64.urlsafe_b64encode(descriptor.model_dump_json().encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    return _stable_identity("lineage", tenant_key, encoded_descriptor)


def _decode_lineage_stable_reference(identity: str) -> _LineageStableReference:
    try:
        encoded_descriptor = identity + "=" * (-len(identity) % 4)
        return _LineageStableReference.model_validate_json(
            base64.urlsafe_b64decode(encoded_descriptor)
        )
    except (ValueError, ValidationError):
        raise CatalogProviderError(
            "OpenMetadata lineage stable reference is invalid",
            classification="invalid_request",
        ) from None


def _stable_reference_kind_and_identity(
    *, tenant_key: str, identity: str
) -> tuple[CatalogObjectKind | None, str]:
    """Decode a provider-owned stable reference without accepting another tenant's reference."""
    for object_kind in ("namespace", "glossary_term", "classification", "lineage"):
        kind_prefix = f"{object_kind}:"
        if identity.startswith(kind_prefix):
            tenant_prefix = f"{object_kind}:{tenant_key}:"
            if identity.startswith(tenant_prefix):
                return object_kind, identity.removeprefix(tenant_prefix)
            raise CatalogProviderError(
                "OpenMetadata stable reference does not belong to the tenant",
                classification="authorization",
            )
    return None, identity


def _object_ref(
    kind: str, tenant_key: str, identity: str, entity: _EntityResponse
) -> CatalogObjectRef:
    return CatalogObjectRef(
        tenant_key=tenant_key,
        stable_identity=_stable_identity(kind, tenant_key, identity),
        normalized_digest=digest(
            {
                "kind": kind,
                "tenant_key": tenant_key,
                "identity": identity,
                "name": entity.name,
            }
        ),
    )


def _remote_identifier_key(reference: CatalogObjectRef) -> tuple[str, str]:
    return (reference.tenant_key, reference.stable_identity)
