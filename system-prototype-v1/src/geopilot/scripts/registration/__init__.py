"""Validate a manually authored local script registration candidate."""

from geopilot.scripts.registration.models import RegistrationCandidate, RegistrationEvidence, ReferenceCase, ReferenceOutput
from geopilot.scripts.registration.validation import RegistrationError, validate_manual_registration

__all__ = [
    "ReferenceCase", "ReferenceOutput", "RegistrationCandidate", "RegistrationError",
    "RegistrationEvidence", "validate_manual_registration",
]
