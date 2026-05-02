# Security Policy

## Scope

Kite Algo is a trading system. It includes application authentication, broker session handling, live and paper execution surfaces, worker tokens, runtime coordination, and post-trade state.

Please treat security issues seriously and report them responsibly.

## What to report

Examples of in-scope issues:

- authentication or authorization bypass
- worker token leakage or misuse
- broker credential exposure
- live-order placement without the expected guards
- paper/live boundary breaks
- injection vulnerabilities or unsafe file/command execution
- secrets exposed in logs, responses, or tracked files

Out of scope for public issue reporting:

- routine support requests
- general setup problems
- feature requests
- non-security bugs with no confidentiality, integrity, or authorization impact

## How to report a vulnerability

Please do **not** open a public GitHub issue for an active vulnerability.

Use one of these private channels instead:

1. GitHub's **Report a vulnerability** / private security advisory flow for this repository, if it is enabled.
2. If that flow is unavailable, contact the repository owner privately through the contact method listed on the owner's GitHub profile.

Please include:

- a clear description of the issue
- steps to reproduce
- affected files, endpoints, or flows
- whether the issue affects live trading, paper mode, worker tokens, or broker credentials
- any proof-of-concept details needed to reproduce safely

## Safe testing rules

- do not test against live trading flows in a destructive way
- do not place real broker orders without explicit maintainer approval
- do not access data that is not yours
- do not publish raw tokens, credentials, or secrets

## Response expectations

The project should acknowledge responsible reports quickly, validate impact, and work toward a fix before public disclosure.

## Operational guidance for users

- keep `.env` files out of version control
- use strong app credentials and a long random `APP_JWT_SECRET`
- keep worker tokens private
- prefer paper mode first when validating new strategies
- review changes carefully around auth, execution, accounting, and runtime ownership
