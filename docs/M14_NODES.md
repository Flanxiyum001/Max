# M14 — device public-key enrollment foundation

This change adds an **internal library**, not an internet-facing login or a
replacement for the existing loopback bootstrap token.

The owner controller (after real owner authentication) can begin one enrollment
per distinct hardware/OS key. The device generates a P-256 private key locally
and sends only its DER/SPKI public key. NodeRegistry creates a pending record
and a 120-second cryptographically random one-use challenge. The device signs
a versioned, owner/device/challenge/purpose-bound message. A valid signature
activates the node. Revocation invalidates pending challenges. Every subsequent
authentication proof uses another one-use server challenge. Raw nonces are not
stored; signatures and private keys are never persisted by MAX.

**Important limitations**

- Public-key possession is **not** hardware attestation, owner identity,
  Face ID / Touch ID, or WebAuthn user verification.
- The library does not expose HTTP enrollment, issue device-bound sessions,
  authorize HTTP requests, or relax the current 127.0.0.1 bind guard.
- A trusted controller must enforce owner presence, enrollment rate limits,
  device/key rotation and fresh high-risk WebAuthn step-up. It must prevent
  enrollment based on a model instruction or a remote self-asserted owner ID.
- Client key-generation integrations for Windows TPM, Arch Linux TPM/OS
  keystore and iPhone Secure Enclave must be reviewed separately.
- An iPhone browser passkey is a WebAuthn credential, **not** automatically
  the same as this P-256 NodeRegistry keypair. Implement credential binding,
  origin/RP-ID validation, signature counter policy, and challenge-use
  enforcement via a vetted WebAuthn server library.
- Add mTLS / proof-of-possession HTTP authorization, TLS termination and a
  protected remote relay before opening the API to devices.
- This migration is additive but the downgrade drops device records.

No remote access or high-risk execution is authorized by this patch.
