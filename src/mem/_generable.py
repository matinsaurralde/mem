"""Apple FM SDK generable types for mem.

This module is intentionally separate from variables.py because
``from __future__ import annotations`` (PEP 563) converts all type
hints to strings, which breaks apple-fm-sdk's @fm.generable decorator
when it tries to resolve nested type references like
``list[CredentialDetection]`` via ``typing.get_type_hints()``.

By keeping these classes in their own module without deferred
annotations, the SDK can inspect the concrete types at decoration time.
"""

try:
    import apple_fm_sdk as fm

    @fm.generable("Detected credential in a shell command")
    class CredentialDetection:
        original_value: str = fm.guide(
            "The literal sensitive value found in the command"
        )
        suggested_name: str = fm.guide(
            "A descriptive variable name like ACME_API_TOKEN"
        )
        reason: str = fm.guide(
            "Why this looks like a credential (e.g., 'JWT token', 'API key')"
        )

    @fm.generable("List of credentials detected in a shell command")
    class CredentialList:
        credentials: list[CredentialDetection] = fm.guide("All detected credentials")

except ImportError:
    # SDK not available — classes won't be used, but module still importable.
    CredentialDetection = None
    CredentialList = None
