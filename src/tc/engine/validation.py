"""Document and DocuSign signature validation.

Implements the validation checks defined in workflow/document_validation.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    check_id: str
    name: str
    passed: bool
    severity: str  # critical, high, medium, low
    details: str = ""


@dataclass
class DocumentValidationReport:
    document_name: str
    envelope_id: str = ""
    all_passed: bool = True
    results: list[ValidationResult] = field(default_factory=list)
    critical_failures: int = 0
    warnings: int = 0

    def add(self, result: ValidationResult) -> None:
        self.results.append(result)
        if not result.passed:
            self.all_passed = False
            if result.severity == "critical":
                self.critical_failures += 1
            else:
                self.warnings += 1


def validate_envelope_from_api(envelope_data: dict) -> DocumentValidationReport:
    """Validate a DocuSign envelope using data from the DocuSign API.

    envelope_data should contain the result of:
        GET /v2.1/accounts/{accountId}/envelopes/{envelopeId}
        GET .../envelopes/{envelopeId}/recipients
        GET .../envelopes/{envelopeId}/recipients/{recipientId}/tabs
    """
    report = DocumentValidationReport(
        document_name=envelope_data.get("emailSubject", "Unknown Document"),
        envelope_id=envelope_data.get("envelopeId", ""),
    )

    # SIG-001: Envelope Completion Status
    status = envelope_data.get("status", "")
    report.add(ValidationResult(
        check_id="SIG-001",
        name="Envelope Completion Status",
        passed=status == "completed",
        severity="critical",
        details=f"Status: {status}" if status != "completed" else "Completed",
    ))

    # SIG-002: All Recipients Completed
    recipients = envelope_data.get("recipients", {})
    signers = recipients.get("signers", [])
    all_signed = all(s.get("status") == "completed" for s in signers)
    unsigned = [s for s in signers if s.get("status") != "completed"]
    report.add(ValidationResult(
        check_id="SIG-002",
        name="All Recipients Completed",
        passed=all_signed,
        severity="critical",
        details="" if all_signed else f"Unsigned: {', '.join(s.get('name', '?') for s in unsigned)}",
    ))

    # SIG-003 through SIG-008: Tab validation
    for signer in signers:
        tabs = signer.get("tabs", {})

        # SIG-003: Signature fields
        sign_tabs = tabs.get("signHereTabs", [])
        for tab in sign_tabs:
            has_value = tab.get("status") == "signed" or bool(tab.get("value"))
            if not has_value:
                report.add(ValidationResult(
                    check_id="SIG-003",
                    name="Signature Field Populated",
                    passed=False,
                    severity="critical",
                    details=f"Missing signature: {signer.get('name', '?')} page {tab.get('pageNumber', '?')}",
                ))

        # SIG-004: Initial fields
        initial_tabs = tabs.get("initialHereTabs", [])
        for tab in initial_tabs:
            has_value = tab.get("status") == "signed" or bool(tab.get("value"))
            if not has_value:
                report.add(ValidationResult(
                    check_id="SIG-004",
                    name="Initial Field Populated",
                    passed=False,
                    severity="critical",
                    details=f"Missing initials: {signer.get('name', '?')} page {tab.get('pageNumber', '?')}",
                ))

        # SIG-005: Date fields
        date_tabs = tabs.get("dateSignedTabs", [])
        for tab in date_tabs:
            if not tab.get("value"):
                report.add(ValidationResult(
                    check_id="SIG-005",
                    name="Date Field Populated",
                    passed=False,
                    severity="high",
                    details=f"Missing date: {signer.get('name', '?')} page {tab.get('pageNumber', '?')}",
                ))

        # SIG-007: Required text fields
        text_tabs = tabs.get("textTabs", [])
        for tab in text_tabs:
            if tab.get("required") == "true" and not tab.get("value"):
                report.add(ValidationResult(
                    check_id="SIG-007",
                    name="Required Text Field Complete",
                    passed=False,
                    severity="high",
                    details=f"Blank field: '{tab.get('tabLabel', '?')}' page {tab.get('pageNumber', '?')}",
                ))

        # SIG-008: Required checkboxes
        checkbox_tabs = tabs.get("checkboxTabs", [])
        for tab in checkbox_tabs:
            if tab.get("required") == "true" and tab.get("selected") != "true":
                report.add(ValidationResult(
                    check_id="SIG-008",
                    name="Required Checkbox Selected",
                    passed=False,
                    severity="medium",
                    details=f"Unchecked: '{tab.get('tabLabel', '?')}' page {tab.get('pageNumber', '?')}",
                ))

    # If no failures were added for tab checks, add passing results
    check_ids_seen = {r.check_id for r in report.results}
    for cid, cname in [
        ("SIG-003", "All Signature Fields Populated"),
        ("SIG-004", "All Initial Fields Populated"),
        ("SIG-005", "All Date Fields Populated"),
        ("SIG-007", "Required Text Fields Complete"),
        ("SIG-008", "Required Checkboxes Selected"),
    ]:
        if cid not in check_ids_seen:
            report.add(ValidationResult(
                check_id=cid, name=cname, passed=True, severity="info",
            ))

    return report
