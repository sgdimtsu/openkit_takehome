"""Structured model output definitions; JSON Schema is generated from these types."""
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, StringConstraints

NonemptyText = Annotated[str, StringConstraints(min_length=1, pattern=r'\S')]
ActionType = Literal['new_order_request', 'amendment_request', 'resend', 'uncertain']


class StructuredOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class CustomerCandidate(StructuredOutput):
    name_as_stated: Optional[NonemptyText]
    account_id_as_stated: Optional[NonemptyText]


class OrderReferences(StructuredOutput):
    customer_po: Optional[NonemptyText]
    orbit_order_number: Optional[NonemptyText]


class ClassificationAction(StructuredOutput):
    email_id: NonemptyText
    customer: CustomerCandidate
    action_type: ActionType
    references: OrderReferences
    related_email_ids: list[NonemptyText]
    reason: NonemptyText
    uncertainties: list[NonemptyText]


class ClassificationOutput(StructuredOutput):
    actions: list[ClassificationAction]
