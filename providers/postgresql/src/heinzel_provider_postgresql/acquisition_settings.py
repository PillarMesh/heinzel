from __future__ import annotations

from datetime import timedelta
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class PostgreSQLSourceObjectDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_object_ref: str = Field(min_length=1)
    schema_name: str = Field(min_length=1)
    table_name: str = Field(min_length=1)
    field_names: tuple[str, ...] = Field(min_length=1)
    key_name: str = Field(min_length=1)
    source_updated_at_field: str = Field(min_length=1)

    @field_validator("schema_name", "table_name", "key_name", "source_updated_at_field")
    @classmethod
    def requires_valid_identifier_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("PostgreSQL identifiers cannot contain NUL")
        return value

    @field_validator("field_names")
    @classmethod
    def requires_valid_field_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("PostgreSQL field names must be unique")
        if any(not field_name or "\x00" in field_name for field_name in value):
            raise ValueError("PostgreSQL identifiers cannot contain NUL")
        return value

    @model_validator(mode="after")
    def requires_declared_key_and_timestamp(self) -> Self:
        if self.key_name not in self.field_names:
            raise ValueError("PostgreSQL key_name must be a declared field")
        if self.source_updated_at_field not in self.field_names:
            raise ValueError("PostgreSQL source_updated_at_field must be a declared field")
        return self


class PostgreSQLAcquisitionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dsn: SecretStr
    connection_handle: str = Field(min_length=1)
    objects: tuple[PostgreSQLSourceObjectDeclaration, ...] = Field(min_length=1)
    unrelated_schema_name: str = Field(min_length=1)
    max_write_transaction_duration: timedelta = Field(gt=timedelta(0))

    @field_validator("unrelated_schema_name")
    @classmethod
    def requires_valid_unrelated_schema(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("PostgreSQL identifiers cannot contain NUL")
        return value

    @model_validator(mode="after")
    def requires_unique_logical_objects(self) -> Self:
        object_refs = tuple(item.logical_object_ref for item in self.objects)
        if len(object_refs) != len(set(object_refs)):
            raise ValueError("PostgreSQL logical object declarations must be unique")
        physical_objects = tuple((item.schema_name, item.table_name) for item in self.objects)
        if len(physical_objects) != len(set(physical_objects)):
            raise ValueError("PostgreSQL physical object declarations must be unique")
        if any(item.schema_name == self.unrelated_schema_name for item in self.objects):
            raise ValueError("unrelated schema must differ from every approved source schema")
        return self
