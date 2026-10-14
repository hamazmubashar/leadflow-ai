"""Lead validation and normalization (milestone M1). Pure Python, no I/O."""

from leadflow_ai.leads.emails import normalize_email
from leadflow_ai.leads.errors import FieldError, LeadValidationError, NormalizationError
from leadflow_ai.leads.models import Lead, LeadSource
from leadflow_ai.leads.phones import DEFAULT_REGION, SUPPORTED_REGIONS, normalize_phone
from leadflow_ai.leads.text import clean_text
from leadflow_ai.leads.validation import validate_lead

__all__ = [
    "DEFAULT_REGION",
    "SUPPORTED_REGIONS",
    "FieldError",
    "Lead",
    "LeadSource",
    "LeadValidationError",
    "NormalizationError",
    "clean_text",
    "normalize_email",
    "normalize_phone",
    "validate_lead",
]
