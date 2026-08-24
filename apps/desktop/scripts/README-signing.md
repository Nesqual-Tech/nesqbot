# Windows code signing

Release builds are signed by **Azure Trusted Signing** (account `YOUR_SIGNING_ACCOUNT`,
profile `YOUR_CERT_PROFILE`, North Europe), not by a certificate on the build machine.

The signature chains to a root Windows already trusts:

```
CN=Nesqual Tech SRL, O=Nesqual Tech SRL, C=RO
  -> CN=Microsoft ID Verified CS EOC CA 04
  -> CN=Microsoft ID Verified Code Signing PCA 2021
  -> CN=Microsoft Identity Verification Root Certificate Authority 2020
```

That last line is the whole point. The previous self-signed certificate reported
`Status: Valid` on the machine that made it — because that machine trusted its own
root — and would still have raised SmartScreen everywhere else. When verifying a
build, check the **chain**, not the status.

## One-time setup on a build machine

1. **.NET 8 runtime.** `Azure.CodeSigning.Dlib.dll` is managed code; without a
   runtime `signtool` exits 3 and prints nothing at all.
   `winget install --id Microsoft.DotNet.Runtime.8 --scope machine`
2. **Trusted Signing client**, unpacked to `C:\nesqsign\client` (override the root
   with `NESQ_SIGN_TOOLS`):
   `https://api.nuget.org/v3-flatcontainer/microsoft.trusted.signing.client/1.0.95/microsoft.trusted.signing.client.1.0.95.nupkg`
   is a zip; extract it.
3. **`C:\nesqsign\metadata.json`**, written **without a BOM**:
   ```json
   {"Endpoint":"https://neu.codesigning.azure.net/","CodeSigningAccountName":"YOUR_SIGNING_ACCOUNT","CertificateProfileName":"YOUR_CERT_PROFILE"}
   ```
   PowerShell 5.1's `Out-File -Encoding utf8` writes a BOM and the dlib will not
   parse the file. Use `[System.IO.File]::WriteAllText` with `UTF8Encoding $false`.
4. **Azure sign-in** as a principal holding **Artifact Signing Certificate Profile
   Signer** on the account. `az login` is enough locally.

## Why the invocation looks the way it does

`tauri.conf.json` runs `C:\Windows\System32\cmd.exe /c ..\scripts\sign-trusted.cmd %1`.

- **`cmd.exe` by absolute path**: Tauri does not resolve `cmd` from `PATH` and
  reports only `failed to run cmd`.
- **Backslashes**: `cmd.exe` reads a leading `../` as a switch and answers
  `'..' is not recognized`, which surfaces as the same unhelpful Tauri error.
- **`..\scripts\`**: the command runs with `src-tauri` as the working directory.

## Certificate lifetime

Each request mints a certificate valid for about three days. Shipped binaries stay
valid because every signature is RFC-3161 timestamped
(`http://timestamp.acs.microsoft.com`). Dropping `/tr` would produce installers that
start failing validation within the week.

## Verifying a build

```powershell
$s = Get-AuthenticodeSignature '<file>'
$c = New-Object System.Security.Cryptography.X509Certificates.X509Chain
$c.ChainPolicy.RevocationMode = 'Online'
$c.Build($s.SignerCertificate)
$c.ChainElements | ForEach-Object { $_.Certificate.Subject }
```

The last element must be **Microsoft Identity Verification Root Certificate
Authority 2020**. If it is `CN=Nesqual Tech SRL`, the build fell back to the old
self-signed certificate and must not ship.
