"""Validated, retained policy shared by preparation, calculation and review."""
import hashlib
import json
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PolicySection(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)


class PolicySource(PolicySection):
    path: str = Field(min_length=1)
    location: str = Field(min_length=1)
    support: str = Field(min_length=1)


class ConflictingSource(PolicySection):
    path: str = Field(min_length=1)
    location: str = Field(min_length=1)
    recorded_value: str = Field(min_length=1)


class VATAuthority(PolicySection):
    decision: str = Field(min_length=1)
    sources: list[PolicySource] = Field(min_length=1)
    conflicting_source: ConflictingSource


class PricingPolicy(PolicySection):
    vat_rate: str
    currency: Literal['GBP']
    rounding: Literal['ROUND_HALF_UP']
    decimal_places: int = Field(ge=0, le=4)
    calculation_order: Literal['round_unit_then_line_then_vat_on_net_total']
    orbit_price_unit: Literal['pence']
    apply_orbit_discount_once: Literal[True]
    contract_overrides_band: Literal[True]
    use_recorded_band_prices: Literal[True]
    vat_authority: VATAuthority

    @field_validator('vat_rate')
    @classmethod
    def rate_in_range(cls, value):
        try:
            rate = Decimal(value)
            valid = rate.is_finite() and Decimal('0') <= rate <= Decimal('1')
        except ArithmeticError:
            valid = False
        if not valid:
            raise ValueError('VAT rate must be a finite decimal fraction between 0 and 1')
        return value


class FindingLevels(PolicySection):
    blocking: str = Field(min_length=1)
    check: str = Field(min_length=1)
    information: str = Field(min_length=1)


class ReviewPolicy(PolicySection):
    manual_credit_check_required: Literal[True]
    credit_authority: Literal['Tom / credit control']
    pricing_delivery_authority: Literal['Karen / operations']
    customer_claims_authorise_exceptions: Literal[False]
    payment_confirmation_for_proforma_required: Literal[True]
    ocr_manual_review_required: Literal[True]
    stock_delivery_guaranteed: Literal[False]
    finding_levels: FindingLevels


class SharedPolicy(PolicySection):
    version: Literal['1']
    pricing: PricingPolicy
    review: ReviewPolicy

    @property
    def vat_rate(self):
        return Decimal(self.pricing.vat_rate)

    @property
    def vat_percent(self):
        return percent_text(self.vat_rate)

    @property
    def fingerprint(self):
        canonical = json.dumps(self.model_dump(), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()


def percent_text(rate):
    text = format(Decimal(rate) * 100, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def load_policy(path=None):
    """Reject unknown/unsupported settings; never silently fall back after an error."""
    resource = Path(path) if path is not None else files('meridian').joinpath('policies/policy.json')
    return SharedPolicy.model_validate(json.loads(resource.read_text(encoding='utf-8')))


def policy_for_record(record, policy=None):
    saved = record.get('shared_policy')
    if saved is not None:
        retained = SharedPolicy.model_validate(saved)
        if record.get('policy_sha256') != retained.fingerprint:
            raise ValueError('Saved policy fingerprint does not match its policy snapshot')
        if policy is not None and policy.fingerprint != retained.fingerprint:
            raise ValueError('Selected policy differs from the preparation policy; prepare again under the new policy')
        return retained
    return policy if policy is not None else load_policy()
