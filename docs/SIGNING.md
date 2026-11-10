# Signing ConcordeAI for macOS

So the app opens with no "can't check this app" warning. The build scripts are
ready (NOTES 6b549). Once your certificate and notary profile are in the keychain,
`./release.sh` signs, notarizes and staples on its own. Until then it ships ad hoc,
exactly as before, and says so.

## Your steps (only you can do these)

1. **Enrol.** Go to [developer.apple.com/programs](https://developer.apple.com/programs/)
   and enrol as an **individual**. It costs $99 a year. Your Apple ID needs two-factor
   authentication turned on. Approval usually takes a day or two.

2. **Make the certificate.** Once you're approved, open Xcode › Settings › Accounts,
   add your Apple ID, select your team, click **Manage Certificates…**, click **+** and
   choose **Developer ID Application**. It goes into your login keychain.

3. **Let the scripts notarize.** At [account.apple.com](https://account.apple.com),
   under Sign-In and Security › App-Specific Passwords, make one called "concorde-notary".
   Then run this yourself in Terminal. It asks for that password, and only you type it:

   ```
   xcrun notarytool store-credentials concorde-notary --apple-id <your Apple ID email> --team-id <your team ID>
   ```

   Your team ID is the 10-character code at developer.apple.com › Account › Membership
   details. Claude never sees or types the password. It stays in your keychain.

4. **Tell Claude "signing is set up".** Claude then checks
   `security find-identity -v -p codesigning` and
   `xcrun notarytool history --keychain-profile concorde-notary` and cuts the next build.

## What the scripts do with it

- `build_shell_dmg.sh` takes the first valid **Developer ID Application** identity in the
  keychain (or `CONCORDE_SIGN_ID`). It signs inside out with the hardened runtime and a
  secure timestamp: the two helpers, then the app with `shell/src-tauri/entitlements.plist`.
- With the `concorde-notary` profile (or `CONCORDE_NOTARY_PROFILE`), it notarizes and staples
  the app, builds the disk image, then signs, notarizes and staples that too. It then asks
  Gatekeeper about both and stops loudly, with Apple's log, if anything is refused.
- `release.sh` prints what it shipped: notarized, Developer ID but not notarized, or ad hoc.
- `CONCORDE_SIGN_ID=-` forces ad hoc. `CONCORDE_SIGN_HARDENED=1` (with no certificate) is
  the rehearsal: ad hoc, but with the hardened runtime and the entitlements.

## Good to know

- The first signed build asks for the microphone once more, even if you allowed it
  before, because macOS sees a new signer. After that, permissions survive updates. Ad hoc
  builds can lose them on every update.
- Nightlies are built on GitHub's Macs and stay ad hoc. Signing them would need the
  certificate and an API key stored as repository secrets. That's a separate decision.

## Windows (research only, nothing set up)

SmartScreen warns about unsigned downloads. Since 2024, no certificate removes the warning
on day one, not even EV. Reputation builds as people download signed files. Signing is still
what lets that reputation build and stick to the app instead of to each file.

| Option | Cost a year | What you'd do |
|---|---|---|
| **Azure Artifact Signing** (was Trusted Signing), Basic | about $120 ($9.99 a month, 5,000 signatures) | Azure account, then identity checks (ID and selfie). Individuals must be in the USA or Canada. Billing starts when the account is made. CI signs with Microsoft's GitHub Action, so there is no token to keep. |
| **OV certificate** (for example Certum's open-source certificate, or Sectigo through a reseller) | about $60–$300\*, plus a USB token or a paid cloud key | Buy the certificate, pass the CA's identity check, and keep the key on a hardware token (required since 2023). CI signing needs the CA's paid cloud service. |

**Recommendation:** Azure Artifact Signing. It's the cheapest option that works from CI,
and Microsoft runs the certificate's renewal. Choose an OV certificate only if
Microsoft's identity check won't accept you, for example because of where you live.

\* Estimated from published reseller prices; check before buying.
