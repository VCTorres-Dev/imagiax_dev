# Payments Specifications — IMAGIAX

_Last updated: 2026-04-04 | Sources: verification-fixes_

---

## Payment Callback Auth Model (VER-008)

Payment callback and redirect endpoints MUST have an explicitly defined authentication model.

### Scenario: Payment success page access
- GIVEN a user attempting to access the payment success page
- WHEN the request is made
- THEN the system MUST require a valid session cookie

### Scenario: Server-to-server webhook access
- GIVEN a payment webhook request
- WHEN the webhook is received
- THEN the system MUST NOT require cookie authentication
- AND it MUST validate the webhook signature
