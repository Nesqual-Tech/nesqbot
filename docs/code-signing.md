# Code signing

## The short version

Releases are signed by **Azure Trusted Signing** with a publicly trusted certificate issued to
Nesqual Tech SRL. UAC names the publisher on any Windows machine, with nothing to install.

The rest of this document explains why that took work, and what to do if it ever has to be redone.

"Unknown publisher" on the UAC prompt has nothing to do with metadata. `bundle.publisher`,
`Manufacturer`, `CompanyName` — all of those are real and correct, and Windows shows them in
Add/Remove Programs. The elevation prompt and SmartScreen ignore them entirely. They read the
**Authenticode signature**, and an unsigned binary has none, so Windows says Unknown.

Only a code signature changes it. There are exactly three ways to get one, and they differ in
who trusts the result.

## What is in place now (2026-09-04)

**Azure Trusted Signing, publicly trusted.** Identity validation passed, so the release build is
signed with a certificate that every Windows machine already trusts — no `.cer` to deploy, and no
"Unknown publisher" anywhere.

| | |
| --- | --- |
| Signing account | `nesqual-signing`, endpoint `https://neu.codesigning.azure.net/` |
| Certificate profile | `nesqual-public` (Public Trust) |
| Subject | `CN=Nesqual Tech SRL, O=Nesqual Tech SRL, L=Rovinari, S=Gorj, C=RO` |
| Issuer | `CN=Microsoft ID Verified CS AOC CA 04, O=Microsoft Corporation, C=US` |
| Wired in | `tauri.conf.json` → `bundle.windows.signCommand` → `scripts/sign-trusted.cmd` |

Tauri signs the app executable and both installers on every `tauri build`. Confirm a build came
out signed:

```powershell
Get-AuthenticodeSignature '...\Nesq Bot_0.11.2_x64-setup.exe' | Format-List Status, SignerCertificate
```

`Status : Valid` with the Microsoft issuer above is the whole check. SmartScreen may still warn on
a brand-new file until reputation accrues; that is reputation, not trust, and it settles on its
own.

The rest of this document is the road that led here, kept because it is the map for renewing,
re-validating, or setting this up for another entity.

## Previously: a self-signed certificate (superseded)

Before validation passed, builds used a **self-signed** certificate created on the build machine:

| | |
| --- | --- |
| Thumbprint | `B3D314FD4FE9FB301428BA4ECA57B260961A7AB2` |
| Subject | `CN=Nesqual Tech SRL, O=Nesqual Tech SRL, C=RO` |
| Key / hash | RSA 3072, SHA-256, Code Signing EKU |
| Expires | 2029-08-22 |
| Public half | `apps/desktop/src-tauri/installer/nesqual-codesign.cer` |

It is trusted in `CurrentUser\Root` and `CurrentUser\TrustedPublisher` on the build machine, and
wired into `tauri.conf.json` via `certificateThumbprint`, so Tauri signs the app executable and
both installers, with an RFC 3161 timestamp.

**What this actually buys, stated honestly:**

- On this machine, and on any machine where the `.cer` is deployed to Trusted Root **and**
  Trusted Publishers, Windows shows **Nesqual Tech SRL** instead of Unknown, and the signature
  verifies.
- On every other machine it is still Unknown, and SmartScreen still warns on download. A
  self-signed certificate asserts an identity that nobody independently checked, and Windows is
  right not to believe it.

So this is a legitimate answer for an internal tool pushed to managed company machines, and it
is **not** an answer for anything downloaded from the internet.

### Deploying the trust internally

Intune: *Devices → Configuration → Trusted certificate*, one profile targeting the Trusted Root
store and one targeting Trusted Publishers, both with `nesqual-codesign.cer`.

Group Policy: *Computer Configuration → Windows Settings → Security Settings → Public Key
Policies*, import into **Trusted Root Certification Authorities** and **Trusted Publishers**.

Both stores matter. Root alone makes the signature *valid*; Trusted Publishers is what stops the
prompt.

## Azure Artifact Signing — how it was set up

**Note the rename:** Trusted Signing is now **Azure Artifact Signing**. The resource provider is
still `Microsoft.CodeSigning` and the CLI extension is `az artifact-signing`.

### Already done (2026-08-22)

| | |
| --- | --- |
| Provider `Microsoft.CodeSigning` | Registered |
| Signing account | `nesqual-signing`, resource group `rg-nesqbot` |
| Region / endpoint | North Europe — `https://neu.codesigning.azure.net/` |
| SKU | Basic (~$10/month) |
| Roles on the account | `Artifact Signing Identity Verifier` and `Artifact Signing Certificate Profile Signer`, both assigned to `avery.v@nesqualtech.com` |

That is everything that can be automated. The next step cannot be.

### What a human has to do — Azure portal only

Identity validation **cannot be done from the CLI**; Microsoft supports it only in the portal, and
part of it is a live identity check against a government ID. In the portal: *Artifact Signing
accounts → `nesqual-signing` → Objects → Identity validations → Organization → New identity →
Public*.

Have ready — and note these must match public records for the legal entity **exactly**, because a
mistake means starting a new request, not editing this one:

- **Organization name** — the legal entity, `Nesqual Tech SRL`
- **Website** — `nesqualtech.com`
- **Primary email** — a monitored mailbox on the company domain. Verification links land here,
  expire in **7 days**, and the mailbox must accept external links.
- **Secondary email** — different address, same domain (a distribution list is fine)
- **Business identifier** — the Romanian company registration number (CUI / Trade Register no.)
- **Registered business address**
- **First and last name** of the representative, spelled exactly as on their government ID

The named representative then completes a **Verified ID** check through Microsoft's partner
(AU10TIX): email PIN, phone number, then a phone-camera scan of a passport or ID card via QR code,
ending in Microsoft Authenticator. Roughly ten minutes, and it needs a phone.

**Processing takes 1 to 20 business days**, longer if Microsoft asks for documents. Any documents
requested must be under 12 months old and valid for at least another 2 months.

### The eligibility risk, stated plainly

Since April 2025, Public Trust organization validation requires **at least three years of
verifiable operating history** — business registration, tax records, D-U-N-S or similar.
Microsoft's own guidance says there is currently **no exception process** for organizations under
three years old, though they have said they intend to open it up eventually.

**If Nesqual Tech SRL is younger than three years, this application will be rejected** and no
amount of setup changes that. In that case the options are: keep the self-signed certificate for
internal machines (below), buy a traditional OV certificate from a commercial CA, which has no
company-age rule, or wait.

Geography is fine — Public Trust is available to EU organizations, and Romania qualifies.

### Status — passed

Identity validation was submitted on 2026-08-22 and **passed**. The three-year risk above did not
materialise. There is still no way to read validation status from outside the portal — the
`artifact-signing` CLI extension exposes only account and certificate-profile commands, and
identity validation is not an ARM resource type, so `az rest` cannot see it either. The indirect
signal is what confirmed it: the Public Trust profile `nesqual-public` exists, and a Public Trust
profile cannot be created before validation completes.

```powershell
az artifact-signing certificate-profile list -g rg-nesqbot --account-name nesqual-signing
```

If this ever has to be redone, watch for an "Action Required" state: the named representative has
to complete the Verified ID check, and the emailed links **expire after 7 days**, at which point
the whole request has to be recreated.

### What was done once validation passed

```powershell
az extension add --name artifact-signing
az artifact-signing certificate-profile create -g rg-nesqbot `
  --account-name nesqual-signing -n nesqual-public `
  --profile-type PublicTrust --identity-validation-id <id from the portal>
```

`tauri.conf.json` then moved off `certificateThumbprint` to
`bundle.windows.signCommand` → `..\scripts\sign-trusted.cmd`, which invokes the Artifact Signing
dlib against the account and profile above. The Intune/GPO trust profiles below are no longer
needed — a publicly trusted certificate needs no client configuration. The self-signed certificate
is kept only as a fallback for building without network access to the signing endpoint.

## The third option, for completeness

A traditional OV or EV certificate from DigiCert/Sectigo. Publicly trusted, several hundred
euro a year, and EV requires a hardware token or cloud HSM. This was the fallback if the
three-year rule had blocked validation. It did not, so it stays unused — Trusted Signing is
cheaper, has no token to lose, and is already adjacent to this stack.

## Verifying a signature

```powershell
Get-AuthenticodeSignature "Nesq Bot_0.11.2_x64-setup.exe" | Format-List Status, SignerCertificate
```

`Status : Valid` with subject `CN=Nesqual Tech SRL` and issuer
`CN=Microsoft ID Verified CS AOC CA 04` means it worked — on any Windows machine, with nothing
installed. `signtool verify /pa /v` reports the same thing with the full chain:

```powershell
& "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe" verify /pa /v "Nesq Bot_0.11.2_x64-setup.exe"
```
