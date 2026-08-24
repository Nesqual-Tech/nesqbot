# Code signing — why Windows says "Unknown publisher", and the path out

## The short version

"Unknown publisher" on the UAC prompt has nothing to do with metadata. `bundle.publisher`,
`Manufacturer`, `CompanyName` — all of those are real and correct, and Windows shows them in
Add/Remove Programs. The elevation prompt and SmartScreen ignore them entirely. They read the
**Authenticode signature**, and an unsigned binary has none, so Windows says Unknown.

Only a code signature changes it. There are exactly three ways to get one, and they differ in
who trusts the result.

## What is in place now (2026-08-22)

A **self-signed** certificate, created on the build machine:

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

## Azure Artifact Signing — provisioned, waiting on identity validation

**Note the rename:** Trusted Signing is now **Azure Artifact Signing**. The resource provider is
still `Microsoft.CodeSigning` and the CLI extension is `az artifact-signing`.

### Already done (2026-08-22)

| | |
| --- | --- |
| Provider `Microsoft.CodeSigning` | Registered |
| Signing account | `YOUR_SIGNING_ACCOUNT`, resource group `rg-nesqbot` |
| Region / endpoint | North Europe — `https://neu.codesigning.azure.net/` |
| SKU | Basic (~$10/month) |
| Roles on the account | `Artifact Signing Identity Verifier` and `Artifact Signing Certificate Profile Signer`, both assigned to `avery@example.test` |

That is everything that can be automated. The next step cannot be.

### What a human has to do — Azure portal only

Identity validation **cannot be done from the CLI**; Microsoft supports it only in the portal, and
part of it is a live identity check against a government ID. In the portal: *Artifact Signing
accounts → `YOUR_SIGNING_ACCOUNT` → Objects → Identity validations → Organization → New identity →
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

### Status

Identity validation was **submitted on 2026-08-22**. There is no way to read its status from
outside the portal — the `artifact-signing` CLI extension exposes only account and
certificate-profile commands, and identity validation is not an ARM resource type, so
`az rest` cannot see it either. Microsoft emails the primary address on status changes, and the
portal shows it under *Identity validations*.

The one signal visible from the CLI is indirect but reliable: a Public Trust certificate profile
cannot be created until validation completes, so

```powershell
az artifact-signing certificate-profile list -g rg-nesqbot --account-name YOUR_SIGNING_ACCOUNT
```

returning a profile — or simply accepting one — means validation has passed.

Expect **1 to 20 business days**. Watch for an "Action Required" state: the named representative
has to complete the Verified ID check, and the emailed links **expire after 7 days**, at which
point the whole request has to be recreated.

### After validation completes

```powershell
az extension add --name artifact-signing
az artifact-signing certificate-profile create -g rg-nesqbot `
  --account-name YOUR_SIGNING_ACCOUNT -n nesqbot-public `
  --profile-type PublicTrust --identity-validation-id <id from the portal>
```

Then swap `tauri.conf.json` from `certificateThumbprint` to a `signCommand` that invokes the
Artifact Signing dlib or `trusted-signing-cli`, delete the self-signed certificate, and remove the
Intune/GPO trust profiles — they become unnecessary the moment the certificate is publicly trusted.

## Interim: the self-signed certificate

Microsoft's signing service, and the right destination given everything else already runs in this
Azure tenant. Roughly $10/month on the Basic tier. Certificates are publicly trusted, so no client
configuration is needed anywhere, and SmartScreen reputation accrues to the identity rather than
to each individual binary.

`Microsoft.CodeSigning` is registered on subscription `NESQUAL` already. Nearest regions to
`swedencentral` are **North Europe** and **West Europe**.

**Why it was not done tonight:** a Public Trust certificate profile requires Microsoft to validate
Nesqual Tech SRL as a legal entity — company registration details, and a verifiable link between
the requester and the organisation. That review takes days and needs a human with the company's
legal documents. It cannot be scripted.

Steps, when someone picks it up:

1. Create a Trusted Signing account in North Europe or West Europe.
2. Create an **Identity Validation** request for Nesqual Tech SRL and supply the registration
   documents. Wait for approval.
3. Create a **Public Trust** certificate profile against the validated identity.
4. Give the build principal the **Trusted Signing Certificate Profile Signer** role.
5. Switch `tauri.conf.json` from `certificateThumbprint` to a `signCommand` invoking
   `trusted-signing-cli` (or Azure's `signtool` dlib), and delete the self-signed certificate
   along with the Intune/GPO trust profiles.

## The third option, for completeness

A traditional OV or EV certificate from DigiCert/Sectigo. Publicly trusted, several hundred
euro a year, and EV requires a hardware token or cloud HSM. Trusted Signing is cheaper, has no
token to lose, and is already adjacent to this stack — the only reason to prefer a traditional CA
is a signing workflow that cannot reach Azure.

## Verifying a signature

```powershell
& "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe" verify /pa /v "Nesq Bot_0.1.0_x64-setup.exe"
```

`Successfully verified` with signer `Nesqual Tech SRL` means it worked. On a machine that does not
trust the self-signed certificate the same command fails — that is the expected result, not a
regression.
