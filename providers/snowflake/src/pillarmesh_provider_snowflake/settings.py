import re

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class SnowflakeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: SecretStr
    role: str
    warehouse: str
    database: str
    schema_name: str
    stage: str
    target_table: str
    ledger_table: str
    connection_handle: str = Field(min_length=1)

    @field_validator(
        "role",
        "warehouse",
        "database",
        "schema_name",
        "stage",
        "target_table",
        "ledger_table",
    )
    @classmethod
    def simple_identifier(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("Snowflake object identifiers must be simple unquoted identifiers")
        return value.upper()
