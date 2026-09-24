from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from heinzel_contract_model import canonical_bytes, digest
from pydantic import ValidationError

from .models import DashboardContract, SignedDashboardContract


class InvalidDashboardContract(RuntimeError):
    pass


def _signing_payload(*, tenant_id: str, contract_digest: str, key_id: str) -> bytes:
    return canonical_bytes(
        {
            "domain": "heinzel-dashboard-contract-v1",
            "tenant_id": tenant_id,
            "contract_digest": contract_digest,
            "key_id": key_id,
        }
    )


class DashboardContractSigner:
    def __init__(self, key_id: str, private_key: Ed25519PrivateKey) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]+", key_id) is None:
            raise ValueError("dashboard contract signing key identifier is invalid")
        self._key_id = key_id
        self._private_key = private_key

    def sign(self, *, tenant_id: str, contract: DashboardContract) -> SignedDashboardContract:
        validated_contract = DashboardContract.model_validate(
            contract.model_dump(mode="python"), strict=True
        )
        contract_digest = digest(validated_contract)
        signature = self._private_key.sign(
            _signing_payload(
                tenant_id=tenant_id,
                contract_digest=contract_digest,
                key_id=self._key_id,
            )
        )
        return SignedDashboardContract(
            tenant_id=tenant_id,
            contract=validated_contract,
            contract_digest=contract_digest,
            key_id=self._key_id,
            signature=base64.b64encode(signature).decode("ascii"),
        )


class DashboardContractVerifier:
    def __init__(self, keys: Mapping[str, Ed25519PublicKey]) -> None:
        self._keys = dict(keys)

    def verify(self, signed: SignedDashboardContract) -> DashboardContract:
        try:
            envelope = SignedDashboardContract.model_validate(
                signed.model_dump(mode="python"), strict=True
            )
            key = self._keys.get(envelope.key_id)
            if key is None:
                raise InvalidDashboardContract("unknown dashboard contract signing key")
            key.verify(
                base64.b64decode(envelope.signature, validate=True),
                _signing_payload(
                    tenant_id=envelope.tenant_id,
                    contract_digest=envelope.contract_digest,
                    key_id=envelope.key_id,
                ),
            )
        except InvalidDashboardContract:
            raise
        except ValidationError as error:
            raise InvalidDashboardContract("dashboard contract envelope is invalid") from error
        except (InvalidSignature, ValueError, binascii.Error) as error:
            raise InvalidDashboardContract("dashboard contract signature is invalid") from error
        return envelope.contract
