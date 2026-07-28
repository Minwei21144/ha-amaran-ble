# Security Policy

## Sensitive data

An exported Amaran configuration contains the Mesh NetKey and AppKey. Anyone
with these keys and suitable radio access may be able to control fixtures in
that Mesh.

Never include exported configuration JSON, Home Assistant access tokens,
private keys or unreviewed Bluetooth captures in a public issue.

Downloaded integration diagnostics redact NetKey and AppKey values and
pseudonymize MAC addresses. Review every attachment before publishing it
because surrounding Home Assistant logs may contain data produced by other
integrations.

## Reporting a vulnerability

Do not open a public issue for an unpatched vulnerability or leaked secret.
Use GitHub's private vulnerability reporting for this repository when
available. If that option is not visible, contact the maintainer through the
GitHub profile and share only the minimum information needed to establish a
private reporting channel.

Include:

- A concise description and impact.
- A minimal reproduction without real Mesh keys, tokens or MAC addresses.
- The affected release and Home Assistant version.
- Any suggested mitigation.

## Compromised Mesh keys

Removing a key from a log or issue does not make it secret again. If real Mesh
keys were exposed, recreate or rotate the Mesh using the official Amaran/Sidus
software, then export a new Home Assistant configuration.
