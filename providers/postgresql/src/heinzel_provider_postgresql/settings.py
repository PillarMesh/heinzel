from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class PostgresSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dsn: SecretStr
    connection_handle: str = Field(min_length=1)
    schema_name: str = Field(min_length=1)
    table_name: str = Field(min_length=1)
    key_name: str = "order_id"

    @field_validator("schema_name", "table_name", "key_name")
    @classmethod
    def valid_identifier_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("identifiers cannot contain NUL")
        return value
