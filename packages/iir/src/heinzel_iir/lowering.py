from heinzel_contract_model import IntegrationContract

from .models import IntentIR


def lower_contract(contract: IntegrationContract) -> IntentIR:
    return IntentIR(
        contract_id=contract.contract_id,
        contract_version=contract.version,
        source_relation=f"{contract.source.schema_name}.{contract.source.table}",
        source_primary_key=contract.source.primary_key,
        projection=contract.projection,
        materialization_mode=contract.materialization_mode,
        deletion_behavior=contract.deletion_behavior,
    )
