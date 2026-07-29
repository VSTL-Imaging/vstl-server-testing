# VSTL Server Codex Laptop Sync Handoff

Last updated: 2026-07-29

Use this file when opening the project from another laptop or another Codex app. GitHub is the shared source of truth so both laptops see the same project instructions, server logic notes, and update history.

## 1. GitHub Account And Repositories

GitHub organization/account:

- `VSTL-Imaging`

Login email / username:

- `ai@vstl.ae`

Important credential rule:

- Do not save the GitHub password in this file, in Git, or in any project document.
- GitHub normally does not accept account passwords for Git push. Use browser authentication, Git Credential Manager, or a GitHub Personal Access Token.
- If a password or token was pasted into chat or saved in a file, rotate it from GitHub immediately.

Repositories:

- Original/main server repo: `https://github.com/VSTL-Imaging/vstl-server-original.git`
- Testing server repo: `https://github.com/VSTL-Imaging/vstl-server-testing.git`

Branches:

- `main` tracks `original/main`
- `testing` tracks `testing-remote/testing`

## 2. Server Roles

Original/main server:

- IP: `10.255.0.75`
- Role: production server / original live setup.
- Policy: do not deploy changes here until testing is approved and the user says `deploy main`.

Testing server:

- IP: `10.255.0.45`
- Role: test server for validating changes before main deployment.
- Network note: this server is intended to use the fiber / 10G SFP+ testing setup where available.
- Policy: apply and validate risky server, PXE, bench, network, report, and QC changes here first.

Workflow rule:

- Test on `10.255.0.45` first.
- Deploy to `10.255.0.75` only after approval.

## 3. What This Project Contains

This repository is the handoff and source-control package for the VSTL imaging server work. It includes server setup notes, bench/PXE instructions, helper scripts, and documentation needed for Codex or another AI coding tool to understand the environment.

Common areas:

- `imaging_server/bench-client/`: VSTL Bench client logic, QC flow, restore/capture/secure erase related client code.
- `imaging_server/reporting/`: local report/export related logic.
- `imaging_server/tools/`: server helper scripts and operational tools.
- `imaging_server/*.md`: setup, recovery, deployment, PXE, and handoff documents.
- `GITHUB_SETUP.md`: short GitHub usage guide.
- `tools/push-to-github.ps1`: PowerShell push helper.
- `tools/push-main-to-github.cmd`: Windows launcher for pushing main/original changes.
- `tools/push-testing-to-github.cmd`: Windows launcher for pushing testing changes.

Ignored / not synced by GitHub:

- `.env` and live secrets
- API keys, passwords, tokens
- ISO files, `.img`, `.qcow2`, `.vhd`, `.vhdx`
- large backup/archive files
- local logs and reports unless intentionally added
- downloaded tools and raw media captures

## 4. First Setup On Another Laptop

Open PowerShell and run:

```powershell
mkdir D:\Projects -ErrorAction SilentlyContinue
cd D:\Projects
git clone https://github.com/VSTL-Imaging/vstl-server-original.git "VSTL Server"
cd "D:\Projects\VSTL Server"
git remote rename origin original
git remote add testing-remote https://github.com/VSTL-Imaging/vstl-server-testing.git
git fetch testing-remote
git checkout -B testing testing-remote/testing
git switch main
git status --short --branch
```

If Git asks for login, complete browser sign-in or use Git Credential Manager / a Personal Access Token.

## 5. Mandatory Codex Startup Rule

Before Codex starts any edit, change, deployment, or investigation on another laptop, it must pull the latest GitHub state first.

For main/original work:

```powershell
cd "D:\Projects\VSTL Server"
git switch main
git pull original main
git fetch testing-remote
git status --short --branch
```

For testing-server work:

```powershell
cd "D:\Projects\VSTL Server"
git switch testing
git pull testing-remote testing
git fetch original
git status --short --branch
```

If there are local uncommitted changes, Codex must inspect them before editing. Do not delete or reset user changes without explicit approval.

## 6. Push Changes After Every Approved Update

Use the `.cmd` launchers because some Windows systems block direct `.ps1` execution.

Push original/main server documentation or code:

```cmd
cd /d "D:\Projects\VSTL Server"
tools\push-main-to-github.cmd "Describe the change here"
```

Push testing server documentation or code:

```cmd
cd /d "D:\Projects\VSTL Server"
tools\push-testing-to-github.cmd "Describe the change here"
```

Alternative PowerShell command if needed:

```powershell
cd "D:\Projects\VSTL Server"
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\push-to-github.ps1 -MainServer -Message "Describe the change here"
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\push-to-github.ps1 -TestingServer -Message "Describe the change here"
```

After pushing, confirm with:

```powershell
git status --short --branch
git log -1 --oneline
```

## 7. Automatic Sync Expectation

GitHub sync is not a background automatic sync unless a separate automation is created later.

Current required behavior:

1. Pull from GitHub before work.
2. Make the change.
3. Commit with a clear message.
4. Push to the correct GitHub repo/branch.
5. Tell the user the commit hash and which repo was updated.

Do not create an automatic scheduled push that commits everything blindly. That can leak secrets, upload large images/ISOs, or publish unfinished work.

## 8. Testing Vs Main Deployment Logic

Testing logic:

- Use branch `testing`.
- Push to `testing-remote/testing`.
- Apply server-side changes to `10.255.0.45`.
- Validate PXE boot, VSTL Bench screen, restore, capture, secure erase, QC, reports, and network behavior.

Main/original logic:

- Use branch `main`.
- Push to `original/main`.
- Apply changes to `10.255.0.75` only after the user says `deploy main`.
- Main server must remain stable and should not receive experimental changes.

Recommended Codex instruction for every new task:

```text
First pull the latest GitHub state for the correct branch. If this is a testing change, work on testing and push to vstl-server-testing. If I say deploy main, push/apply only the approved change to main/original.
```

## 9. Handoff Checklist For Another Codex App

When opening this project on another laptop, Codex should:

1. Read this file completely.
2. Read `GITHUB_SETUP.md`.
3. Run `git remote -v`.
4. Run `git status --short --branch`.
5. Pull the latest branch before doing work.
6. Avoid committing secrets and large binary artifacts.
7. Push after every approved change.
8. Report the updated repo, branch, and commit hash.

## 10. Quick Troubleshooting

PowerShell script blocked:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\push-to-github.ps1 -MainServer -Message "Describe change"
```

Wrong branch:

```powershell
git branch --show-current
git switch main
git switch testing
```

Check remote setup:

```powershell
git remote -v
```

Pull latest safely:

```powershell
git status --short
git pull --ff-only
```

If `git pull --ff-only` fails, stop and ask before merging or rebasing.
