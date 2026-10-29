"""Versioned, finite UTC tariff schedules. Never substitutes a reserve for settlement."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Rates(Strict):
    input_uncached: float = Field(ge=0)
    input_cached: float = Field(ge=0)
    output: float = Field(ge=0)

    def cost(self, usage):
        counts = (max(usage['prompt'] - usage['cached'], 0), usage['cached'], usage['completion'])
        return float(sum(Decimal(str(rate)) * count for rate, count in
                         zip((self.input_uncached, self.input_cached, self.output), counts)) / 1_000_000)


def utc(value):
    if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
        raise ValueError('pricing timestamps must have explicit UTC offset')
    return value.astimezone(timezone.utc)


class Period(Strict):
    starts_at: datetime
    ends_at: datetime
    tariff_id: str = Field(min_length=1, max_length=100)
    rates: Rates

    _utc = field_validator('starts_at', 'ends_at')(utc)

    @model_validator(mode='after')
    def ordered(self):
        if self.starts_at >= self.ends_at:
            raise ValueError('empty pricing interval')
        return self


class Schedule(Strict):
    version: Literal[1] = 1
    route_id: str
    model: str
    pricing_version: str = Field(min_length=1, max_length=120)
    currency: Literal['USD'] = 'USD'
    unit: Literal['per_million_tokens'] = 'per_million_tokens'
    cost_kind: Literal['estimate'] = 'estimate'
    time_basis: Literal['gate_admission_utc'] = 'gate_admission_utc'
    provider_confirmed: Literal[False] = False
    source_url: str = Field(pattern=r'^https://', max_length=1000)
    calendar_source_url: str = Field(pattern=r'^https://', max_length=1000)
    checked_at: datetime
    valid_from: datetime
    valid_until: datetime
    # Explicit coverage; no client inference about holidays, weekdays or DST.
    periods: list[Period] = Field(min_length=1, max_length=5000)
    excluded_charges: list[Literal['search', 'fetch', 'image', 'other']] = Field(default_factory=list)

    _utc = field_validator('checked_at', 'valid_from', 'valid_until')(utc)

    @model_validator(mode='after')
    def coverage(self):
        if self.valid_from >= self.valid_until or self.checked_at > self.valid_until:
            raise ValueError('invalid pricing validity')
        cursor = self.valid_from
        for period in self.periods:
            if period.starts_at != cursor:
                raise ValueError('pricing intervals must be sorted, contiguous and non-overlapping')
            cursor = period.ends_at
        if cursor != self.valid_until:
            raise ValueError('pricing intervals must cover the complete validity horizon')
        return self

    def select(self, at: datetime):
        return next((p for p in self.periods if p.starts_at <= at < p.ends_at), None)

    def public(self):
        doc = self.model_dump(mode='json')
        doc['reserve_rates'] = {key: max(getattr(p.rates, key) for p in self.periods)
                                for key in Rates.model_fields}
        return doc


def selected(schedule, period):
    return {'version': 1, 'route_id': schedule.route_id, 'model': schedule.model,
            'pricing_version': schedule.pricing_version, 'tariff_id': period.tariff_id,
            'starts_at': period.starts_at.isoformat(), 'ends_at': period.ends_at.isoformat(),
            'rates': period.rates.model_dump(), 'currency': schedule.currency,
            'unit': schedule.unit, 'cost_kind': 'estimate', 'time_basis': schedule.time_basis,
            'provider_confirmed': False, 'excluded_charges': schedule.excluded_charges}
