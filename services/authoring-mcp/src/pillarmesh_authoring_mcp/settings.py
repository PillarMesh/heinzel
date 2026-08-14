from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PILLARMESH_", extra="forbid", frozen=True, env_file=None
    )

    state_path: Path
    output_dir: Path
    signing_key_id: str = Field(min_length=1)
    signing_private_key_b64: SecretStr

    postgres_dsn: SecretStr
    postgres_connection_handle: str
    postgres_schema: str
    postgres_table: str

    snowflake_account: str
    snowflake_user: str
    snowflake_password: SecretStr
    snowflake_role: str
    snowflake_warehouse: str
    snowflake_database: str
    snowflake_schema: str
    snowflake_stage: str
    snowflake_target_table: str
    snowflake_ledger_table: str
    snowflake_connection_handle: str
