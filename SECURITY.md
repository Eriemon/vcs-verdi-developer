# Security Policy

## Supported Versions

Security fixes target the latest `main` branch unless a dedicated release branch is explicitly announced.

## Reporting a Vulnerability

Please use GitHub private vulnerability reporting if it is enabled for this repository. If that is unavailable, open a minimal public issue requesting a private follow-up channel and do not include exploit details, secrets, real license values, or private infrastructure information.

## What Counts

- Secret exposure, credential leakage, or unsafe logging.
- Publication of private hostnames, internal paths, or license details.
- Unsafe command construction or archive handling in helper scripts.
- Validation logic that can falsely report VCS, Verdi, or remote ThreadFPGA acceptance.
- Documentation that encourages unsafe handling of SSH, tokens, proprietary tools, or private environment data.

## Handling Expectations

We will acknowledge valid reports, reproduce them in a minimal environment, and publish fixes with clear notes. Do not include real tokens, private keys, private server names, private network details, or proprietary design data in a report.
