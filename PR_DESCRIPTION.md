## Summary

Fixes Critical and High priority security vulnerabilities identified in security audit (March 22, 2026).

## Changes

### 🔴 Critical
- **Header Injection via CRLF**: Added `_sanitize_header()` function to validate all header values (lines 227-237)
- Prevents BCC injection and arbitrary header manipulation

### 🟠 High  
- **STARTTLS Certificate Verification**: Added SSL context to `starttls()` call (lines 257-259)
  - Previously vulnerable to MITM attacks
- **SMTP Envelope Handling**: Use `email.utils.parseaddr` to extract bare addresses (lines 246, 255)
  - Fixes issue where display names like `"Name <addr>"` were passed raw to SMTP envelope

## Security Audit

Full audit details available in companion issue. This PR addresses the Critical and High priority items.

**Co-authored-by**: Claude <claude@anthropic.com>
