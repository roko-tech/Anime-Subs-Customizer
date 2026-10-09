# ADR-005: Trust and code signing

- Status: accepted (2026-10-09)

## Context

An unsigned exe downloaded from GitHub meets Edge's "isn't commonly downloaded" warning, then SmartScreen's "Windows protected your PC". Verified in Windows Sandbox on 2026-10-09: a Mark-of-the-Web file started from Explorer shows the SmartScreen prompt, and started by another program it shows "Open File – Security Warning: Unknown Publisher". On Windows 11, **Smart App Control** (when on) can block unsigned apps outright, with no "Run anyway". The app also rewrites many files, a pattern ransomware heuristics watch for.

Signing options for a maintainer in Saudi Arabia (researched 2026-10-02):
- Azure Artifact Signing is not offered to individuals outside the US or Canada.
- EV certificates no longer bypass SmartScreen, and OV certificates need hardware tokens and cost money every year.
- **SignPath Foundation** signs open-source projects for free. It requires an already released, maintained project, builds on GitHub-hosted runners, MFA, manual approval of each release, and a privacy rule.

## Decision

- **v1.2.0 ships unsigned but verifiable:**
  - `SHA256SUMS.txt` for every asset;
  - a **GitHub build-provenance attestation** for every asset (`actions/attest`, on the public repo);
  - a CycloneDX **SBOM**;
  - an **immutable release**, so a published release's assets and tag can't change;
  - users verify with `gh attestation verify <file> -R roko-tech/Anime-Subs-Customizer` or `gh release verify-asset`.
- **A bilingual guide**, `docs/install-help.md` and `docs/install-help.ar.md`, linked from both READMEs and the release notes. It explains:
  - Edge "…isn't commonly downloaded" → "…" → Keep → "Show more" → "Keep anyway";
  - SmartScreen "Windows protected your PC" → More info → Run anyway;
  - **Smart App Control**: why it may block an unsigned app with no override, what turning it off costs (on many Windows 11 versions it can't simply be switched back on; the guide states Microsoft's current behaviour, re-checked at release time), and the honest trade-off. The recommendation is to wait for a signed release rather than turn off a protection;
  - antivirus false positives, and how to check the file's SHA-256 against `SHA256SUMS.txt`.
  It uses screenshots from the Sandbox runs.
- **Pluggable signing step:** `release.yml` has a `sign` job that runs only when the repository variable `SIGNING_PROVIDER` is set (`signpath`). Otherwise it is skipped and the unsigned files flow on. Every attestation and checksum is computed **after** the optional signing step.
- **After the release,** prepare the SignPath Foundation application: the project description, the signing policy page (`docs/signing-policy.md`), the privacy statement (no telemetry; the update check is off by default, ADR-008) and the CI details. Submitting it and the MFA setup are the maintainer's steps (hand-off to-do list).

## Consequences

- Early users see one or two warnings. The guide and the verifiable artefacts reduce the risk that they give up or fall for a fake copy.
- Smart App Control users may not be able to run v1.2.0 at all. This is documented honestly, and signing is the real fix (tracked as a public issue).

## Verification

- Sandbox evidence of each prompt is screenshotted into the guide (matrix row 1, before and after publish).
- After publishing: `gh attestation verify`, `gh release verify-asset` and `sha256sum -c SHA256SUMS.txt` all pass on the downloaded assets (DoD).
- VirusTotal results for the installer, the exe and the ZIP are linked in the release report.
