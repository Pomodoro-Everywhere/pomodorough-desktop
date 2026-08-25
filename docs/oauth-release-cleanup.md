# Rotate the Desktop OAuth credential and scan release artifacts

Use this checklist after an operator can access both Google Cloud Console and the `Pomodoro-Everywhere/pomodorough-desktop` GitHub repository. Do not paste the compromised secret into an issue, commit, workflow input, or shell argument.

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
6. Verify Google sign-in with one built Desktop artifact.
7. Delete the old production OAuth client in Google Cloud Console. Do not delete the final replacement client.
8. Remove the old client ID from `GOOGLE_NATIVE_CLIENT_IDS` and deploy the server again.
9. Verify that a token for the old client is rejected and a token for the new client is accepted.

Record the Google audit-log event IDs, the server deployment ID, and the verification time in the private release record. Do not record either secret.

## Download the exact release artifacts

Set the tag and download the published assets. The release workflow publishes these exact names.

```bash
export REPOSITORY=Pomodoro-Everywhere/pomodorough-desktop
export TAG=v0.4.1
export VERSION="${TAG#v}"
export EXPECTED_OAUTH_CLIENT_ID='614768274539-a70rconcgcn51ksk37ud352cra2ccb7r.apps.googleusercontent.com'
mkdir -p "scan-$VERSION"
gh release download "$TAG" --repo "$REPOSITORY" --dir "scan-$VERSION"
cd "scan-$VERSION"
sha256sum --check SHA256SUMS.txt
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

## Close the blocked external release cleanup

Close the external OAuth release-cleanup item only when all of these facts are recorded in the private release record:

- Google revoked the compromised secret or deleted its old OAuth client.
- Production accepts the retained or replacement Desktop client ID.
- A built Desktop artifact completed Google sign-in without a client secret.
- The downloaded Flatpak and Windows assets match `SHA256SUMS.txt` and their GitHub attestations.
- The unpacked Flatpak and PyInstaller payloads contain no non-empty `client_secret` and no byte sequence equal to the compromised secret.
