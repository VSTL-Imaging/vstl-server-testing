# VSTL Server GitHub Setup

This folder is prepared as a local Git repository.

## Repositories to create

Create these as private repositories under the GitHub account/organization:

- `vstl-server-original`
- `vstl-server-testing`

## Local branches

- `main` represents the original/main server project snapshot.
- `testing` represents the testing server project snapshot.

## Add GitHub remotes

After the repositories exist, run these commands from `D:\Projects\VSTL Server`:

```powershell
git remote add original https://github.com/VSTL-Imaging/vstl-server-original.git
git remote add testing-remote https://github.com/VSTL-Imaging/vstl-server-testing.git
```

If the repositories are created under a different GitHub owner, replace `VSTL-Imaging` with that owner name.

## First upload

```powershell
git push -u original main
git push -u testing-remote testing
```

GitHub requires a Personal Access Token or GitHub Credential Manager login for push. Account passwords are not accepted for Git pushes.

## Future updates

For original/main server updates:

```powershell
.\tools\push-to-github.ps1 -MainServer -Message "Describe the change"
```

For testing server updates:

```powershell
git switch testing
.\tools\push-to-github.ps1 -TestingServer -Message "Describe the change"
```

The repository intentionally ignores live `.env` files, ISO images, raw disk images, downloads, backups, and local KVM screenshots.
