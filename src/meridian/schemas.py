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


def validate_action_ids(output, email_ids):
    """Validate source references for both fresh and saved classifications."""
    selected = set()
    for action in output.actions:
        if action.email_id not in email_ids:
            raise ValueError(f'Classification references an unknown email ID: {action.email_id}')
        if action.email_id in selected:
            raise ValueError(f'Classification repeats email ID: {action.email_id}')
        selected.add(action.email_id)
        if (len(action.related_email_ids) != len(set(action.related_email_ids)) or
                any(related not in email_ids or related == action.email_id
                    for related in action.related_email_ids)):
            raise ValueError(f'Classification has invalid related email IDs for {action.email_id}')
    return selected
