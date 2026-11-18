"""Per-email proposal schema. Calculations and final readiness belong to code."""
from typing import Literal, Optional

from pydantic import Field, model_validator

from .schemas import ActionType, NonemptyText, StructuredOutput


class SupportedValue(StructuredOutput):
    value: NonemptyText = Field(description='Requested or suggested value as text; preserve raw dates and amounts.')
    basis: Literal['explicit_request', 'recorded_internal', 'inferred_suggestion']
    evidence_ids: list[NonemptyText] = Field(min_length=1)
    reason: Optional[NonemptyText] = None


class QuotedPrice(StructuredOutput):
    amount: SupportedValue
    currency: NonemptyText
    vat_basis: Literal['exclusive', 'inclusive', 'unknown']
    price_basis: Literal['per_unit', 'line_total', 'order_total', 'unknown']


class CustomerDiscount(StructuredOutput):
    description: SupportedValue
    scope: Optional[SupportedValue] = None


class OrderLineProposal(StructuredOutput):
    line_id: NonemptyText
    description: SupportedValue
    sku: Optional[SupportedValue] = None
    quantity: Optional[SupportedValue] = None
    unit: Optional[SupportedValue] = None
    quantity_basis: Literal['order_total']
    customer_quoted_price: Optional[QuotedPrice] = None


class OrderHeaderProposal(StructuredOutput):
    delivery_destination: Optional[SupportedValue] = None
    requested_delivery_date: Optional[SupportedValue] = None
    delivery_instructions: Optional[SupportedValue] = None


class NewOrderProposal(StructuredOutput):
    header: OrderHeaderProposal
    lines: list[OrderLineProposal]
    customer_quoted_totals: list[QuotedPrice]
    customer_discounts: list[CustomerDiscount]


class ChangeTarget(StructuredOutput):
    orbit_order_number: Optional[int] = None
    orbit_line_number: Optional[int] = None
    sku: Optional[SupportedValue] = None


class ProposedChange(StructuredOutput):
    change_id: NonemptyText
    operation: Literal['replace', 'add_line', 'remove_line', 'cancel_order']
    field: NonemptyText
    target: Optional[ChangeTarget] = None
    proposed_value: Optional[SupportedValue] = None
    unit: Optional[SupportedValue] = None
    quantity_basis: Optional[Literal['replacement_total', 'additional']] = None
    added_line: Optional[OrderLineProposal] = None
    evidence_ids: list[NonemptyText] = Field(min_length=1)

    @model_validator(mode='after')
    def check_operation(self):
        if self.operation == 'replace' and self.proposed_value is None:
            raise ValueError('Replacement requires a proposed value')
        if self.operation == 'add_line' and self.added_line is None:
            raise ValueError('Adding a line requires its proposed details')
        if self.operation != 'add_line' and self.added_line is not None:
            raise ValueError('Added-line details belong only to add_line')
        if self.field == 'line_quantity' and self.operation == 'replace' and self.quantity_basis is None:
            raise ValueError('Quantity changes must distinguish replacement total from additional quantity')
        return self


class AmendmentProposal(StructuredOutput):
    changes: list[ProposedChange] = Field(min_length=1)


class ResendProposal(StructuredOutput):
    related_email_ids: list[NonemptyText]
    explanation: NonemptyText
    evidence_ids: list[NonemptyText] = Field(min_length=1)


class UnresolvedAction(StructuredOutput):
    possible_action_types: list[ActionType]
    explanation: NonemptyText
    evidence_ids: list[NonemptyText]


class PreparationUncertainty(StructuredOutput):
    question: NonemptyText
    affected_fields: list[NonemptyText]
    next_action: NonemptyText
    evidence_ids: list[NonemptyText]


class PreparationIssue(StructuredOutput):
    category: NonemptyText
    description: NonemptyText
    affected_fields: list[NonemptyText]
    next_action: NonemptyText
    owner: Optional[NonemptyText] = None
    evidence_ids: list[NonemptyText] = Field(min_length=1)


class PreparationOutput(StructuredOutput):
    email_id: NonemptyText
    action_type: ActionType
    summary: NonemptyText
    customer_po: Optional[SupportedValue] = None
    new_order: Optional[NewOrderProposal] = None
    amendment: Optional[AmendmentProposal] = None
    resend: Optional[ResendProposal] = None
    unresolved_action: Optional[UnresolvedAction] = None
    uncertainties: list[PreparationUncertainty]
    issues: list[PreparationIssue]

    @model_validator(mode='after')
    def check_action_section(self):
        section = {'new_order_request': 'new_order', 'amendment_request': 'amendment',
                   'resend': 'resend', 'uncertain': 'unresolved_action'}[self.action_type]
        present = [name for name in ('new_order', 'amendment', 'resend', 'unresolved_action')
                   if getattr(self, name) is not None]
        if present != [section]:
            raise ValueError('Exactly one action-specific section must match action_type')
        return self
