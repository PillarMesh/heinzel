from __future__ import annotations


class FulfillmentError(ValueError):
    pass


class FulfillmentStaleRevision(FulfillmentError):
    pass


class FulfillmentOwnershipError(FulfillmentError):
    pass


class FulfillmentGroundingError(FulfillmentError):
    pass


class FulfillmentPolicyError(FulfillmentError):
    pass


class FulfillmentAuthorityError(FulfillmentError):
    pass


class FulfillmentIntegrityError(FulfillmentError):
    pass


class FulfillmentNotVisible(FulfillmentError):
    pass
