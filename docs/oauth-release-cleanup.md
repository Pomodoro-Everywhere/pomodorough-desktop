# Rotate the Desktop OAuth credential and scan release artifacts

Use this checklist after an operator can access both Google Cloud Console and the `Pomodoro-Everywhere/pomodorough-desktop` GitHub repository. Do not paste the compromised secret into an issue, commit, workflow input, or shell argument.

## P2.2 acceptance status: open

The target is suite **0.10.0** (`v0.10.0`). These commands apply only after the Desktop release is published and its downloaded assets are independently verified. Previously reviewed sign-off implementation and automated tests are not production acceptance evidence for this candidate. Source tests, deterministic artifact self-tests, and this checklist cannot close P2.2.

Remaining external work: obtain the three assets from one immutable release revision, verify provenance and payloads, run real production OAuth and secure restoration on each required platform, then retain private receipts and rollout/revocation evidence. No Google, account, Linux, or Windows production sign-off is recorded by this preparation. Do not reuse older-release receipts.

## Rotate or revoke the compromised credential

The final bundled Desktop OAuth client ID is stored in `src/pomodorough/resources/oauth-client.json`. The app uses PKCE and does not need a client secret. Treat these as three distinct credentials throughout cleanup:

- **Old production client:** retained temporarily so already-released clients continue to work.
- **Exposed intermediate client:** any client created or shown during rotation that is not the final bundled client. Delete it immediately after identifying it from the Google audit log; never infer it from the current resource file.
- **Final replacement client:** the client in the release commit. Keep it and validate it with a packaged-artifact sign-in.

1. Open Google Cloud Console for the Pomodorough project.
2. Go to **Google Auth Platform > Clients**.
3. Open the old production client identified in the private rotation record, not the final client currently stored in `src/pomodorough/resources/oauth-client.json`.
4. If Google offers **Reset secret**, reset the secret. Do not save or distribute the replacement secret.
5. Verify that Google marks the old secret as revoked or unusable.
6. Run a Desktop Google sign-in with the client-secret-free `oauth-client.json`.

If Google does not offer an in-place reset, replace the client:

1. Create an OAuth client of type **Desktop app**.
2. Add the new client ID to the production server's `GOOGLE_NATIVE_CLIENT_IDS`. Keep the old ID during the transition.
3. Deploy the server and verify that it accepts an ID token whose audience is the new client ID.
4. Replace only `client_id` in `src/pomodorough/resources/oauth-client.json`. Do not add `client_secret`.
5. Build the wheel, source archive, Flatpak bundle, and Windows executable from that commit.
6. Complete the downloaded wheel, Flatpak, and Windows production sign-offs below and retain their private evidence before ending the transition.
7. Delete the old production OAuth client in Google Cloud Console. Do not delete the final replacement client.
8. Remove the old client ID from `GOOGLE_NATIVE_CLIENT_IDS` and deploy the server again.
9. Verify that a token for the old client is rejected and a token for the new client is accepted.

Record the Google audit-log event IDs, the server deployment ID, and the verification time in the private release record. Do not record either secret.

## Download the exact release artifacts

After the release exists, set the tag and download the published assets. Run Bash examples from the desktop checkout on Linux (Bash 4+); `scan-$VERSION` must be new. The release workflow publishes these exact names.

```bash
export REPOSITORY=Pomodoro-Everywhere/pomodorough-desktop
export TAG=v0.10.0
export VERSION="${TAG#v}"
export EXPECTED_OAUTH_CLIENT_ID='614768274539-a70rconcgcn51ksk37ud352cra2ccb7r.apps.googleusercontent.com'
mkdir -p "scan-$VERSION"
gh release download "$TAG" --repo "$REPOSITORY" --dir "scan-$VERSION"
cd "scan-$VERSION"
sha256sum --check SHA256SUMS.txt
gh attestation verify SHA256SUMS.txt --repo "$REPOSITORY"
gh attestation verify "pomodorough_linux-$VERSION-py3-none-any.whl" --repo "$REPOSITORY"
gh attestation verify "Pomodorough-$VERSION-x86_64.flatpak" --repo "$REPOSITORY"
gh attestation verify "Pomodorough-$VERSION-windows-x86_64.exe" --repo "$REPOSITORY"
```

Confirm that the release contains exactly these six files:

```text
Pomodorough-<version>-windows-x86_64.exe
Pomodorough-<version>-x86_64.flatpak
pomodorough_linux-<version>-py3-none-any.whl
pomodorough_linux-<version>.tar.gz
pomodorough-desktop.spdx.json
SHA256SUMS.txt
```

Retain the release URL/tag, resolved full commit SHA, attestation workflow/revision, exact filenames, checksums, and verification results in the private release record. Require provenance for all three tested assets and the checksum manifest to identify that same release revision. Confirm `EXPECTED_OAUTH_CLIENT_ID` against `src/pomodorough/resources/oauth-client.json` at that revision, not a dirty checkout. Stop on any mismatch or missing evidence.

## Load the compromised secret without exposing it

Read the compromised value from a password manager into an environment variable. This command does not put the value in shell history.

```bash
IFS= read -rsp 'Compromised Google client secret: ' COMPROMISED_GOOGLE_CLIENT_SECRET
printf '\n'
export COMPROMISED_GOOGLE_CLIENT_SECRET
```

Clear the variable when the scans finish:

```bash
unset COMPROMISED_GOOGLE_CLIENT_SECRET
```

## Scan the Flatpak payload

Install `flatpak` and `ostree`, then unpack the bundle without running the app.

```bash
mkdir flatpak-repo
ostree init --repo=flatpak-repo --mode=archive-z2
flatpak build-import-bundle flatpak-repo "Pomodorough-$VERSION-x86_64.flatpak"
FLATPAK_REF="$(ostree --repo=flatpak-repo refs | grep '^app/me.egigoka.Pomodorough/' | head -n 1)"
test -n "$FLATPAK_REF"
ostree --repo=flatpak-repo checkout "$FLATPAK_REF" flatpak-root
```

Verify every packaged OAuth document and scan every unpacked byte for the compromised value:

```bash
mapfile -t OAUTH_FILES < <(find flatpak-root -name oauth-client.json -type f -print)
test "${#OAUTH_FILES[@]}" -gt 0
for file in "${OAUTH_FILES[@]}"; do
  jq -e --arg expected "$EXPECTED_OAUTH_CLIENT_ID" '(.installed // .web // .) | (.client_id == $expected) and ((.client_secret // "") == "")' "$file" >/dev/null
done
python3 ../scripts/scan_secret.py flatpak-root
```

The commands must find at least one `oauth-client.json`, every `jq` check must return `true`, and `scan_secret.py` must print `secret not found`.

## Scan the Windows PyInstaller payload

Extract the executable before scanning it. A raw scan of the `.exe` can miss compressed PyInstaller members.

```bash
mkdir windows-root
cd windows-root
uvx --from pyinstxtractor-ng pyinstxtractor-ng "../Pomodorough-$VERSION-windows-x86_64.exe"
cd ..
mapfile -t OAUTH_FILES < <(find windows-root -name oauth-client.json -type f -print)
test "${#OAUTH_FILES[@]}" -gt 0
for file in "${OAUTH_FILES[@]}"; do
  jq -e --arg expected "$EXPECTED_OAUTH_CLIENT_ID" '(.installed // .web // .) | (.client_id == $expected) and ((.client_secret // "") == "")' "$file" >/dev/null
done
python3 ../scripts/scan_secret.py windows-root
```

The commands must find at least one `oauth-client.json`, every `jq` check must return `true`, and `scan_secret.py` must print `secret not found`.

## Verify OAuth behavior from release artifacts

Run the deterministic transaction verifier before any production sign-in. It uses controlled callback and API responses. It exercises the packaged OAuth contract, the loopback callback listener, token persistence, a child-process restart, refresh, `/api/v1/me`, and rejected responses. It does not prove that Google or the production API accepts the released client.

For an installed wheel or source distribution, run:

```bash
python -m pomodorough.oauth_artifact_verifier --self-test
```

For the installed Flatpak, run:

```bash
flatpak run --user --command=python3 \
  me.egigoka.Pomodorough \
  -m pomodorough.oauth_artifact_verifier --self-test
flatpak run --user --command=python3 \
  me.egigoka.Pomodorough \
  -m pomodorough.oauth_artifact_verifier --platform-store-self-test
```

The second Flatpak command writes random bytes through Secret Service, launches a second packaged process to read and delete them, and then verifies absence in the parent. The command prints only a boolean result. It fails if the sandbox cannot access `org.freedesktop.secrets` or the runtime lacks `secret-tool`.

For the Windows executable, run both packaged modes in PowerShell:

```powershell
$VERSION = "0.10.0"
$env:POMODOROUGH_OAUTH_ARTIFACT_SELF_TEST = "1"
$oauth = Start-Process ".\Pomodorough-$VERSION-windows-x86_64.exe" -PassThru -Wait
if ($oauth.ExitCode -ne 0) { throw "OAuth artifact self-test failed" }
Remove-Item Env:POMODOROUGH_OAUTH_ARTIFACT_SELF_TEST

$env:POMODOROUGH_PLATFORM_STORE_SELF_TEST = "1"
$store = Start-Process ".\Pomodorough-$VERSION-windows-x86_64.exe" -PassThru -Wait
if ($store.ExitCode -ne 0) { throw "DPAPI self-test failed" }
Remove-Item Env:POMODOROUGH_PLATFORM_STORE_SELF_TEST
```

The second Windows mode verifies a DPAPI save followed by load and delete in a second packaged process, then confirms absence in the parent. Neither mode prints the value.

## Record production sign-off receipts

Run production sign-off only after the three downloaded assets pass checksum, attestation, unpacked-resource, and secret scans. Use the same dedicated Google test account for every artifact. The command opens the real Google authorization page, exchanges the result with `https://pomodorough.egigoka.me`, validates production `/api/v1/me`, starts a second packaged process to restore the same account from the platform secret store, deletes the local credential, and logs out the server session.

Each successful command writes a receipt containing the release version, asset SHA-256, public OAuth client ID, production API origin, platform, completion time, boolean checks, and a one-way account-ID fingerprint. It never writes an access token, refresh token, Google token, email, name, avatar URL, client secret, or device ID. Keep receipts outside every repository: the fingerprint is credential-free but still correlates one account across runs.

The artifact label and digest are operator-supplied; the receipt does not independently hash the running package or verify its attestation. Bind each run to the verified installed asset with a private record of its executable/interpreter path, install command, host OS/architecture, operator, UTC time, exit status, self-test results, and receipt path. Do not substitute a source checkout or another installed version. The `--self-test` uses controlled responses; only the production sign-off commands exercise Google and the production API. Clear OAuth overrides and the compromised-secret environment variable before execution.

Required acceptance cells (all pending until independently evidenced):

| Receipt `artifact` | Downloaded asset | Execution host and receipt `platform` |
| --- | --- | --- |
| `wheel` | `pomodorough_linux-0.10.0-py3-none-any.whl` | Linux desktop; `system=Linux`, `machine` matches the recorded host architecture |
| `flatpak` | `Pomodorough-0.10.0-x86_64.flatpak` | Linux x86_64, installed bundle with Secret Service; `system=Linux`, `machine=x86_64` |
| `windows` | `Pomodorough-0.10.0-windows-x86_64.exe` | Windows x86_64 with DPAPI; `system=Windows`, `machine=AMD64` |

A macOS wheel run is not Linux evidence. One wheel host does not certify another architecture; record each additional supported release acceptance host separately. The source archive is a payload-scan target, not a replacement for any of these three cells.

On Linux, create a private record directory outside the checkout. Install and verify the downloaded wheel itself:

```bash
umask 077
set -o noclobber
export PRIVATE_RECORD="$HOME/pomodorough-private-release-$VERSION"
mkdir -m 700 "$PRIVATE_RECORD"
WHEEL="pomodorough_linux-$VERSION-py3-none-any.whl"
WHEEL_SHA256="$(awk -v asset="$WHEEL" '$2 == asset { print $1 }' SHA256SUMS.txt)"
test "${#WHEEL_SHA256}" -eq 64
python3 -m venv wheel-signoff
wheel-signoff/bin/python -m pip install "./$WHEEL"
wheel-signoff/bin/python -m pomodorough.oauth_artifact_verifier --self-test
wheel-signoff/bin/python -m pomodorough.oauth_artifact_verifier --platform-store-self-test
wheel-signoff/bin/python -m pomodorough.oauth_production_signoff \
  --artifact wheel \
  --asset-sha256 "$WHEEL_SHA256" \
  --receipt - >"$PRIVATE_RECORD/wheel.json"
chmod 600 "$PRIVATE_RECORD/wheel.json"
```

Install the downloaded Flatpak, run both Flatpak self-tests above against that installation, then sign off from its packaged Python process. Stop if either self-test fails. Shell redirection writes the receipt on the host, outside the Flatpak sandbox:

```bash
FLATPAK_ASSET="Pomodorough-$VERSION-x86_64.flatpak"
FLATPAK_SHA256="$(awk -v asset="$FLATPAK_ASSET" '$2 == asset { print $1 }' SHA256SUMS.txt)"
test "${#FLATPAK_SHA256}" -eq 64
flatpak install --user "./$FLATPAK_ASSET"
flatpak run --user --command=python3 me.egigoka.Pomodorough \
  -m pomodorough.oauth_artifact_verifier --self-test
flatpak run --user --command=python3 me.egigoka.Pomodorough \
  -m pomodorough.oauth_artifact_verifier --platform-store-self-test
flatpak run --user --command=python3 \
  me.egigoka.Pomodorough \
  -m pomodorough.oauth_production_signoff \
  --artifact flatpak \
  --asset-sha256 "$FLATPAK_SHA256" \
  --receipt - >"$PRIVATE_RECORD/flatpak.json"
chmod 600 "$PRIVATE_RECORD/flatpak.json"
```

On Windows, verify the transferred/downloaded executable against the same authenticated checksum manifest and attestation, then run both Windows self-tests above. Create a private directory outside the checkout, restrict its ACL to the operator, and launch that executable in production sign-off mode. The receipt path must not already exist:

```powershell
$VERSION = "0.10.0"
$Asset = ".\Pomodorough-$VERSION-windows-x86_64.exe"
$PrivateRecord = Join-Path $HOME "pomodorough-private-release-$VERSION"
$Receipt = Join-Path $PrivateRecord "windows.json"
New-Item -ItemType Directory -Force $PrivateRecord | Out-Null
if (Test-Path $Receipt) { throw "Receipt already exists: $Receipt" }
$env:POMODOROUGH_OAUTH_PRODUCTION_SIGNOFF = "1"
$env:POMODOROUGH_OAUTH_ASSET_SHA256 = (Get-FileHash $Asset -Algorithm SHA256).Hash
$env:POMODOROUGH_OAUTH_SIGNOFF_RECEIPT = $Receipt
try {
  $signoff = Start-Process $Asset -PassThru -Wait
  if ($signoff.ExitCode -ne 0) { throw "Production OAuth sign-off failed" }
} finally {
  Remove-Item Env:POMODOROUGH_OAUTH_PRODUCTION_SIGNOFF
  Remove-Item Env:POMODOROUGH_OAUTH_ASSET_SHA256
  Remove-Item Env:POMODOROUGH_OAUTH_SIGNOFF_RECEIPT
}
```

Stop on any failed command; shell snippets are not an unattended acceptance runner. Inspect the three private JSON receipts. Require `schemaVersion=1`, `releaseTag=v0.10.0`, `artifactVersion=0.10.0`, `production.apiBase=https://pomodorough.egigoka.me`, the release's `production.oauthClientId`, and the same nonempty `production.accountFingerprint`. Require the exact artifact/platform cells above, match each `assetSha256` to its corresponding filename in `SHA256SUMS.txt`, and check `completedAt` against the recorded run time. All five named checks must be present and `true`: `googleSignIn`, `productionAccount`, `secureRestoration`, `serverLogout`, and `localCredentialRemoval`.

On sign-off failure, cleanup is attempted but not guaranteed. No successful receipt is produced; stdout redirection may leave an empty file, and a file's existence alone proves nothing. Record the sanitized failure privately, resolve cleanup, and repeat with a new receipt path. Session logout in a receipt is not evidence that Google revoked the old OAuth client or that the production allowlist was updated.

## Close the blocked external release cleanup

Close the external OAuth release-cleanup item only when all of these facts are recorded in the private release record:

- Google revoked the compromised secret or deleted its old OAuth client.
- Production accepts the retained or replacement Desktop client ID.
- A downloaded wheel, Flatpak, and Windows artifact each complete Google sign-in without a client secret.
- Each artifact validates the authenticated account returned by production `/api/v1/me`; the headless sign-off receipt does not certify account rendering in the UI.
- Each artifact restores the account after the process exits and starts again.
- Private sign-off receipts and execution records bind those checks to the same release revision, exact assets, required platforms, and account fingerprint.
- The downloaded wheel, Flatpak, and Windows assets match `SHA256SUMS.txt` and their GitHub attestations.
- The unpacked Flatpak and PyInstaller payloads contain no non-empty `client_secret` and no byte sequence equal to the compromised secret.
- Raw assets and all unpacked wheel, source archive, Flatpak, and Windows payloads pass the release scan procedure in `scripts/unpack_release_artifacts.sh` and `scripts/verify_release_artifacts.sh`; retain sanitized results.
- The private rollout record identifies the chosen cleanup branch and includes operator/time, Google audit-log event IDs, server deployment IDs, and the branch-specific evidence below. Do not retain tokens or credential-bearing responses.
  - **Replacement client:** record deletion of the old OAuth client, removal of its ID from production `GOOGLE_NATIVE_CLIENT_IDS`, and post-deployment verification that the old audience is rejected and the replacement audience is accepted.
  - **Retained-client secret reset:** record evidence that Google revoked or made the compromised secret unusable and that production still accepts the same retained client ID/audience. This branch does not require client deletion, audience removal, or rejection of that retained audience.
- A fresh independent checker reviews each acceptance cell and the private rollout evidence. Missing, failed, or unexecuted cells remain open; documentation and source verification alone are not sign-off.
