# TalkToHermes for iOS

The native SwiftUI client is in [`TalkToHermes/`](TalkToHermes/). It targets iOS 17 or later and connects only to a TalkToHermes Voice Bridge over authenticated HTTPS.

## Build

1. Open `TalkToHermes/TalkToHermes.xcodeproj` in Xcode.
2. Select your own development team locally if device signing requires it. Do not commit personal team or provisioning data.
3. Select an iOS 17+ simulator or device and build the `TalkToHermes` scheme.

The repository intentionally contains no installation-specific server, signing identity, bearer token, private CA, or private key.

## Configure a bridge

Open **Settings** in the app and enter values supplied by the bridge administrator. Example:

| Field | Example | Notes |
|---|---|---|
| Server | `bridge.example.com` | Hostname only |
| Port | `8443` | Integer from `1` through `65535` |
| Token | administrator-provided value | Stored in the iOS Keychain |

Enter only the hostname in **Server**. Do not enter `https://`, a port, URL credentials, a path, a query string, or a fragment. The app trims whitespace, lowercases the hostname, removes a trailing dot, and validates every DNS label. HTTPS is fixed and cannot be disabled.

When **Save** is tapped, the app first calls the authenticated `GET /v1/status` endpoint. It commits the normalized server, port, response style, spoken language, and Keychain token only after the bridge returns a ready status. The authenticated response also supplies the instance identity displayed by the app.

Host and port are persisted in app preferences. The bearer token is stored separately in the Keychain with `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`; it is never stored in preferences or source code.

## Languages

The app provides German and English localizations. iOS exposes the normal per-app language selection after installation. App language and spoken language are independent: **Settings → Conversation → Spoken language** selects `German (de)` or `English (en)` for subsequent voice turns. Existing installations with no valid spoken-language setting default to German; the app never derives the voice-turn language from device region or UI localization.

## Endpoint and token safety

The saved token is bound operationally to the saved HTTPS origin:

- leaving the token field empty reuses the Keychain token only when the normalized server and port are unchanged;
- changing either server or port requires the token to be entered explicitly;
- failed validation or a failed status check leaves the previously saved endpoint and token unchanged;
- redirects are followed only when scheme, host, and port remain identical, preventing an authenticated request from being redirected to another origin;
- changing the saved endpoint rebuilds the API client and clears the current conversation binding.

The app uses normal `URLSession` certificate validation. It has no certificate-pinning exception, insecure transport fallback, or trust bypass.

## Private certificate authorities

A private bridge may use a private CA. Install and verify only the CA certificate on the managed device, then explicitly enable trust under **Settings → General → About → Certificate Trust Settings**. Never package a private CA certificate or private key in the app, and never disable certificate verification.

The operational CA export, fingerprint verification, and device-installation procedure is documented in [the deployment guide](../deployment/README.md#install-and-trust-the-internal-root-ca-on-ios).

## Verification

Before distributing a build:

1. run the complete active Xcode test plan;
2. build the project with no errors or navigator warnings;
3. save a valid endpoint and complete one voice turn on a real device;
4. terminate and relaunch the app, then complete a second voice turn without re-entering unchanged settings;
5. confirm that changing server or port requires explicit token entry.
